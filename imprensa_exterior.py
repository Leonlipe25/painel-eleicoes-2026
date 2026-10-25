#!/usr/bin/env python3
"""Parciais publicadas por veículos de imprensa, separadas de qualquer resultado oficial.

Este módulo NÃO extrai votos do TSE, NÃO faz previsão e NÃO atribui o carimbo
horário da consulta HTTP à produção editorial da notícia.
"""
from __future__ import annotations

from html.parser import HTMLParser
from html import unescape
from pathlib import Path
import json
import os
import re
import threading
import time
import urllib.request
import unicodedata

FONTE = 'Poder360'
URL = 'https://www.poder360.com.br/poder-eleicoes-2026/compare-as-votacoes-de-lula-e-flavio-no-1o-e-no-2o-turno-no-exterior/'
G1 = 'https://g1.globo.com/mundo/noticia/2026/10/25/votacao-no-exterior-confira-os-resultados-do-2o-turno-entre-lula-e-flavio-bolsonaro.ghtml'
BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('PAINEL_DATA_DIR', str(BASE))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE = DATA_DIR / '.imprensa_exterior_cache.json'
INTERVALO = 180
PUBLICADO_EM = '2026-10-25 11:39 (Brasília)'

# Fotografia editorial verificada na reportagem do Poder360 consultada em 25/10/2026;
# NÃO é dado oficial, nem apuração completa, nem atualização automática garantida.
# Campos: país, votos Lula, votos Flávio. Não se somam a BUs coletados.
BASELINE = [
 ('Coreia do Sul',148,82),('China',502,267),('Taiwan',94,130),
 ('Filipinas',27,39),('Japão',7098,28538),('Malásia',41,44),
 ('Austrália',6017,3465),('Nova Zelândia',418,101),('Singapura',213,121),
 ('Indonésia',15,23),('Tailândia',56,60),('Emirados Árabes Unidos',643,772),
 ('Arábia Saudita',49,31),('Catar',159,195),('Jordânia',725,76),
 ('Egito',60,16),('Kuwait',62,17),('Quênia',29,16),('Síria',106,11),
 ('África do Sul',186,256),('Chipre',29,25),('Estônia',160,33),
 ('Finlândia',550,185),('Grécia',255,331),('Moçambique',108,117),
 ('Líbano',2736,1276),('Palestina',1000,46),('Israel',166,699),
 ('Croácia',31,26),('Eslovênia',67,28),('Polônia',268,89),
 ('República Tcheca',304,86),
]

def normal(t):
    return ''.join(c for c in unicodedata.normalize('NFKD',t.casefold()) if not unicodedata.combining(c)).strip(' * \n\t')
NOMES={normal(x[0]):x[0] for x in BASELINE}
NOMES['qatar']='Catar'
NOMES['republica checa']='República Tcheca'
NOMES['republica tcheca']='República Tcheca'

class Sections(HTMLParser):
    """Conserva títulos e texto de blocos por país; ignora scripts e estilos."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.head=None
        self.heading_depth=0
        self.heading_text=[]
        self.skip=0
        self.sections=[]
        self.current=None
        self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):
            self.skip+=1
        if self.skip:return
        if tag in ('h2','h3','h4'):
            self.head=tag;self.heading_depth+=1;self.heading_text=[]
        if tag in ('p','li','div','br','h2','h3','h4'):
            self.parts.append(' ')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):
            self.skip=max(0,self.skip-1)
            return
        if self.skip:return
        if self.head==tag and tag in ('h2','h3','h4'):
            titulo=normal(''.join(self.heading_text))
            if titulo in NOMES:
                if self.current is not None:
                    self.sections.append((self.current,' '.join(self.parts)))
                self.current=NOMES[titulo];self.parts=[]
            self.head=None;self.heading_depth=0
        if tag in ('p','li','div','br','h2','h3','h4'):
            self.parts.append(' ')
    def handle_data(self,d):
        if self.skip:return
        if self.head:self.heading_text.append(d)
        if self.current is not None:self.parts.append(d)
    def finish(self):
        if self.current is not None:self.sections.append((self.current,' '.join(self.parts)))
        return self.sections

def _votes(segment,nome):
    pattern=rf'\b{nome}\b(?:\s+(?:Bolsonaro|da Silva))?\s*(?:\([^)]{{1,15}}\))?\s*[\-–—:]?\s*(\d[\d\.]*)\s*votos\b'
    m=re.search(pattern,segment,re.I)
    return int(m.group(1).replace('.','')) if m else None

def parse_page(html):
    p=Sections();p.feed(html);out={}
    for pais,txt in p.finish():
        # Remove 1º turno: nunca extrair inadvertidamente votos do turno anterior.
        bloco=re.split(r'\b1\s*[º°o]?\s*turno\b',txt,maxsplit=1,flags=re.I)[0]
        m=re.search(r'\b2\s*[º°o]?\s*turno\b',bloco,re.I)
        if not m:continue
        bloco=bloco[m.end():]
        l=_votes(bloco,'Lula'); f=_votes(bloco,'Flávio')
        if l is not None and f is not None and 0<=l<=1000000 and 0<=f<=1000000:
            out[pais]=(l,f)
    if len(out)<4:
        raise ValueError('A matéria não apresentou blocos de resultados suficientes para uma leitura segura')
    return out

def rows(records):
    return [dict(pais=p,lula=int(l),flavio=int(f),validos=int(l)+int(f),
                 lula_pct=round(100*l/(l+f),2) if l+f else 0,
                 flavio_pct=round(100*f/(l+f),2) if l+f else 0)
            for p,(l,f) in sorted(records.items(),key=lambda x:-(x[1][0]+x[1][1]))]

class Imprensa:
    def __init__(self,refresh=INTERVALO):
        self.refresh=refresh;self.lock=threading.Lock();self.next_try=0;self.checked_at=None
        self.changed_at=None;self.erro=None;self.online=False
        self.dados={p:(l,f) for p,l,f in BASELINE};self.origem='referencia_publicada'
        # Persistência da última leitura bem-sucedida, sem converter cache em dado oficial.
        try:
            value=json.loads(CACHE.read_text(encoding='utf8'))
            if value.get('url')==URL and len(value.get('dados',{}))>=4:
                self.dados={k:tuple(v) for k,v in value['dados'].items() if k in NOMES.values()}
                self.changed_at=value.get('alteracao_observada_em')
                self.origem='ultima_leitura_local'
        except (OSError,ValueError,TypeError):pass

    def atualizar(self):
        # Resposta HTTP local imediata: uma página lenta da imprensa nunca
        # deve travar a interface, os resultados oficiais ou o coletor BU.
        with self.lock:
            if time.monotonic() >= self.next_try:
                self.next_try=time.monotonic()+self.refresh
                threading.Thread(target=self._buscar,daemon=True,name='imprensa-exterior').start()
            return self.panorama_sem_lock()

    def _buscar(self):
        try:
            req=urllib.request.Request(URL,headers={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36','Accept':'text/html,application/xhtml+xml'})
            with urllib.request.urlopen(req,timeout=8) as r:
                if 'text/html' not in r.headers.get('Content-Type','text/html'):
                    raise ValueError('Resposta inesperada, não é HTML')
                raw=r.read(2_000_000)
            parsed=parse_page(raw.decode('utf8',errors='replace'))
            with self.lock:
                antes=self.dados
                # Não conservar países retirados de uma edição da matéria;
                # qualquer exclusão editorial deve refletir na leitura seguinte.
                merged=dict(parsed)
                if merged!=antes:self.changed_at=time.time()
                self.dados=merged;self.online=True;self.checked_at=time.time();self.erro=None
                self.origem='artigo_consultado'
                try:CACHE.write_text(json.dumps(dict(url=URL,dados=merged,alteracao_observada_em=self.changed_at),ensure_ascii=False),encoding='utf8')
                except OSError:pass
        except Exception as exc:
            with self.lock:
                self.erro=f'{type(exc).__name__}: {str(exc)[:140]}'
                self.online=False

    def panorama_sem_lock(self):
        return dict(tipo='levantamento_jornalistico_nao_oficial',fonte=FONTE,url_fonte=URL,url_g1=G1,
                    dados_publicados_a_partir_de=PUBLICADO_EM,
                    consulta_realizada_em=self.checked_at,
                    alteracao_observada_em=self.changed_at,
                    acesso_pagina_nesta_tentativa=self.online,origem=self.origem,
                    erro_ultima_consulta=self.erro,proxima_verificacao_segundos=self.refresh,
                    paises=rows(self.dados),
                    aviso=('Votos noticiados por veículo jornalístico. Horário original de cada país não foi informado. '
                           'Se a página não puder ser lida, mostra a última referência registrada, NÃO dados ao vivo. '
                           'Não somar com Boletins de Urna, nem com a totalização do TSE.'))

    def panorama(self):
        with self.lock:return self.panorama_sem_lock()

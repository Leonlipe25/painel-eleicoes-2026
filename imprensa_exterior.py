#!/usr/bin/env python3
"""Parciais jornalísticas do exterior, sempre separadas da apuração oficial.

O resumo principal é lido do G1. A tabela por país do Poder360 permanece como
referência secundária identificada, sem somar seus números aos do G1, aos BUs
ou à totalização oficial do TSE.
"""
from __future__ import annotations

from html.parser import HTMLParser
from html import unescape
from pathlib import Path
from datetime import datetime, timedelta
import json
import os
import re
import threading
import time
from urllib.parse import urlparse
import urllib.request
import unicodedata
from zoneinfo import ZoneInfo

FONTE = 'G1'
G1 = 'https://g1.globo.com/mundo/noticia/2026/10/25/votacao-no-exterior-confira-os-resultados-do-2o-turno-entre-lula-e-flavio-bolsonaro.ghtml'
G1_SITEMAP = 'https://g1.globo.com/sitemap/g1/sitemap.xml'
PODER360 = 'https://www.poder360.com.br/poder-eleicoes-2026/compare-as-votacoes-de-lula-e-flavio-no-1o-e-no-2o-turno-no-exterior/'
BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('PAINEL_DATA_DIR', str(BASE))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE = DATA_DIR / '.imprensa_exterior_cache.json'
INTERVALO = 180
INTERVALO_DESCOBERTA = 300
PODER_PUBLICADO_EM = '2026-10-25 11:39 (Brasília)'

# Última fotografia verificada antes da publicação desta versão. O coletor
# substitui estes valores assim que consegue ler a matéria do G1.
G1_BASELINE = dict(
    lula=106944,
    flavio=72066,
    vitorias_lula=41,
    vitorias_flavio=12,
    paises_territorios=53,
    atualizado_em='2026-10-25T16:08:25.583-03:00',
)

# Fotografia editorial da reportagem do Poder360 consultada em 25/10/2026.
# Campos: país, votos Lula, votos Flávio. É apenas a tabela secundária.
PODER_BASELINE = [
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
    return ''.join(c for c in unicodedata.normalize('NFKD', t.casefold()) if not unicodedata.combining(c)).strip(' * \n\t')


NOMES = {normal(x[0]): x[0] for x in PODER_BASELINE}
NOMES['qatar'] = 'Catar'
NOMES['republica checa'] = 'República Tcheca'
NOMES['republica tcheca'] = 'República Tcheca'


class VisibleText(HTMLParser):
    """Extrai texto visível suficiente para localizar o resumo editorial."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        elif not self.skip and tag in ('p', 'h1', 'h2', 'br', 'li'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in ('p', 'h1', 'h2', 'li'):
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        return ' '.join(''.join(self.parts).split())


def _numero(valor):
    return int(valor.replace('.', '').replace(',', ''))


def parse_g1(html):
    parser = VisibleText()
    parser.feed(html)
    texto = unescape(parser.text())
    totais = re.search(
        r'Lula\s*[-–—:]\s*([\d.]+)\s*votos\s*Fl[aá]vio(?:\s+Bolsonaro)?\s*[-–—:]\s*([\d.]+)\s*votos',
        texto,
        re.I,
    )
    lula = re.search(r'Lula\s+(?:havia\s+)?(?:vencido|venceu|vence)\s+em\s+(\d+)\s+pa[ií]ses', texto, re.I)
    flavio = re.search(r'Fl[aá]vio(?:\s+Bolsonaro)?(?:,|\s)+(?:havia\s+)?(?:vencido|venceu|vence)?\s*(?:em\s+)?(\d+)\s+pa[ií]ses', texto, re.I)
    if not totais or not lula or not flavio:
        raise ValueError('O resumo atualizado do G1 não pôde ser identificado com segurança')
    data = re.search(r'<meta\s+itemprop=["\']dateModified["\']\s+content=["\']([^"\']+)', html, re.I)
    vl, vf = int(lula.group(1)), int(flavio.group(1))
    return dict(
        lula=_numero(totais.group(1)),
        flavio=_numero(totais.group(2)),
        vitorias_lula=vl,
        vitorias_flavio=vf,
        paises_territorios=vl + vf,
        atualizado_em=data.group(1) if data else None,
    )


class Sections(HTMLParser):
    """Conserva títulos e texto de blocos por país do Poder360."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.head = None
        self.heading_text = []
        self.skip = 0
        self.sections = []
        self.current = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        if self.skip:
            return
        if tag in ('h2', 'h3', 'h4'):
            self.head = tag
            self.heading_text = []
        if tag in ('p', 'li', 'div', 'br', 'h2', 'h3', 'h4'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if self.head == tag and tag in ('h2', 'h3', 'h4'):
            titulo = normal(''.join(self.heading_text))
            if titulo in NOMES:
                if self.current is not None:
                    self.sections.append((self.current, ' '.join(self.parts)))
                self.current = NOMES[titulo]
                self.parts = []
            self.head = None
        if tag in ('p', 'li', 'div', 'br', 'h2', 'h3', 'h4'):
            self.parts.append(' ')

    def handle_data(self, data):
        if self.skip:
            return
        if self.head:
            self.heading_text.append(data)
        if self.current is not None:
            self.parts.append(data)

    def finish(self):
        if self.current is not None:
            self.sections.append((self.current, ' '.join(self.parts)))
        return self.sections


def _votes(segment, nome):
    pattern = rf'\b{nome}\b(?:\s+(?:Bolsonaro|da Silva))?\s*(?:\([^)]{{1,15}}\))?\s*[-–—:]?\s*(\d[\d.]*)\s*votos\b'
    match = re.search(pattern, segment, re.I)
    return int(match.group(1).replace('.', '')) if match else None


def parse_poder360(html):
    parser = Sections()
    parser.feed(html)
    result = {}
    for pais, texto in parser.finish():
        bloco = re.split(r'\b1\s*[º°o]?\s*turno\b', texto, maxsplit=1, flags=re.I)[0]
        segundo = re.search(r'\b2\s*[º°o]?\s*turno\b', bloco, re.I)
        if not segundo:
            continue
        bloco = bloco[segundo.end():]
        lula, flavio = _votes(bloco, 'Lula'), _votes(bloco, 'Flávio')
        if lula is not None and flavio is not None and 0 <= lula <= 1_000_000 and 0 <= flavio <= 1_000_000:
            result[pais] = (lula, flavio)
    if len(result) < 4:
        raise ValueError('A tabela do Poder360 não apresentou resultados suficientes')
    return result


def rows(records):
    return [dict(pais=p, lula=int(l), flavio=int(f), validos=int(l) + int(f),
                 lula_pct=round(100 * l / (l + f), 2) if l + f else 0,
                 flavio_pct=round(100 * f / (l + f), 2) if l + f else 0)
            for p, (l, f) in sorted(records.items(), key=lambda x: -(x[1][0] + x[1][1]))]


def _baixar_resposta(url, tipos=('text/html',), limite=2_500_000, timeout=12):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml,text/xml;q=0.9',
        'Accept-Encoding': 'identity',
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        content_type = response.headers.get('Content-Type', '').lower()
        if tipos and not any(tipo in content_type for tipo in tipos):
            raise ValueError(f'Resposta em formato inesperado: {content_type or "sem Content-Type"}')
        return response.read(limite).decode('utf8', errors='replace'), response.geturl()


def _baixar(url):
    return _baixar_resposta(url)[0]


def _url_g1_valida(url):
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.hostname == 'g1.globo.com'
            and parsed.path.endswith('.ghtml') and '/google/amp/' not in parsed.path)


def _amp_url(url):
    parsed = urlparse(url)
    if parsed.hostname != 'g1.globo.com':
        raise ValueError('URL fora do domínio do G1')
    return f'https://g1.globo.com/google/amp{parsed.path}'


def _data_iso(valor):
    try:
        return datetime.fromisoformat(valor or '').timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def _pontuar_url_g1(url):
    """Restringe a descoberta a matérias inequivocamente ligadas ao placar."""
    slug = normal(url.replace('-', ' ').replace('/', ' '))
    pontos = 0
    pontos += 9 if 'exterior' in slug else 0
    pontos += 3 if 'votacao' in slug else 0
    pontos += 3 if 'resultad' in slug else 0
    pontos += 2 if '2o turno' in slug or 'segundo turno' in slug else 0
    pontos += 2 if 'lula' in slug else 0
    pontos += 2 if 'flavio' in slug else 0
    pontos += 2 if '/mundo/' in url else 0
    return pontos


def descobrir_urls_g1(agora=None):
    """Localiza possíveis substitutas usando somente os sitemaps oficiais do G1."""
    agora = agora or datetime.now(ZoneInfo('America/Sao_Paulo'))
    seed_date = re.search(r'/(\d{4}/\d{2}/\d{2})/', G1)
    datas = [(agora - timedelta(days=i)).strftime('%Y/%m/%d') for i in range(3)]
    if seed_date and seed_date.group(1) not in datas:
        datas.append(seed_date.group(1))
    # A consulta direta evita baixar o índice completo (cerca de 1 MB) em cada
    # rodada. O índice continua como plano B caso o G1 altere a segmentação.
    mapas = [f'https://g1.globo.com/sitemap/g1/{data}_1.xml' for data in datas]
    candidatos = {}
    consultados = set()
    for mapa in mapas:
        consultados.add(mapa)
        try:
            conteudo, _ = _baixar_resposta(
                mapa, tipos=('application/xml', 'text/xml'), limite=2_000_000, timeout=20)
        except Exception:
            continue
        for url in re.findall(r'<loc>\s*([^<]+)', conteudo, re.I):
            url = unescape(url.strip())
            if _url_g1_valida(url):
                pontos = _pontuar_url_g1(url)
                if pontos >= 12:
                    candidatos[url] = max(pontos, candidatos.get(url, 0))
        if candidatos:
            break
    if not candidatos:
        xml, _ = _baixar_resposta(
            G1_SITEMAP, tipos=('application/xml', 'text/xml'), limite=2_000_000, timeout=20)
        extras = [unescape(x) for x in re.findall(r'<loc>\s*([^<]+)', xml, re.I)]
        for mapa in [u for u in extras if u not in consultados and any(f'/{data}_' in u for data in datas)][:8]:
            try:
                conteudo, _ = _baixar_resposta(
                    mapa, tipos=('application/xml', 'text/xml'), limite=2_000_000, timeout=20)
            except Exception:
                continue
            for url in re.findall(r'<loc>\s*([^<]+)', conteudo, re.I):
                url = unescape(url.strip())
                if _url_g1_valida(url):
                    pontos = _pontuar_url_g1(url)
                    if pontos >= 12:
                        candidatos[url] = max(pontos, candidatos.get(url, 0))
    return [url for url, _ in sorted(candidatos.items(), key=lambda item: (-item[1], item[0]))]


def obter_g1(url):
    """Lê a página normal e usa a versão AMP oficial se a primeira falhar."""
    erros = []
    for modo, alvo in (('pagina_normal', url), ('pagina_amp', _amp_url(url))):
        try:
            html, final = _baixar_resposta(alvo)
            resumo = parse_g1(html)
            canonical = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', html, re.I)
            resolvida = unescape(canonical.group(1)) if canonical else final
            if not _url_g1_valida(resolvida):
                resolvida = url
            return resumo, resolvida, modo
        except Exception as exc:
            erros.append(f'{modo}: {type(exc).__name__}: {str(exc)[:120]}')
    raise ValueError('; '.join(erros))


class Imprensa:
    def __init__(self, refresh=INTERVALO):
        self.refresh = refresh
        self.lock = threading.Lock()
        self.next_try = 0
        self.next_discovery = 0
        self.buscando = False
        self.checked_at = None
        self.discovery_at = None
        self.changed_at = None
        self.g1_online = False
        self.poder_online = False
        self.erro_g1 = None
        self.erro_descoberta = None
        self.erro_poder = None
        self.resumo_g1 = dict(G1_BASELINE)
        self.dados_poder = {p: (l, f) for p, l, f in PODER_BASELINE}
        self.url_g1 = G1
        self.modo_g1 = 'referencia_publicada'
        self.origem_g1 = 'referencia_publicada'
        try:
            value = json.loads(CACHE.read_text(encoding='utf8'))
            resumo = value.get('resumo_g1', {})
            url_cache = value.get('url_g1', G1)
            if _url_g1_valida(url_cache) and resumo.get('vitorias_lula') is not None:
                self.resumo_g1 = resumo
                self.url_g1 = url_cache
                self.modo_g1 = value.get('modo_g1', 'ultima_leitura_local')
                self.origem_g1 = 'ultima_leitura_local'
            poder = value.get('dados_poder', {})
            if value.get('url_poder360') == PODER360 and len(poder) >= 4:
                self.dados_poder = {k: tuple(v) for k, v in poder.items() if k in NOMES.values()}
            self.changed_at = value.get('alteracao_observada_em')
        except (OSError, ValueError, TypeError):
            pass

    def atualizar(self):
        with self.lock:
            if time.monotonic() >= self.next_try and not self.buscando:
                self.next_try = time.monotonic() + self.refresh
                self.buscando = True
                threading.Thread(target=self._buscar, daemon=True, name='imprensa-exterior').start()
            return self.panorama_sem_lock()

    def _buscar(self):
        novo_g1 = None
        nova_url = None
        novo_modo = None
        novo_poder = None
        erro_g1 = None
        erro_descoberta = None
        erro_poder = None
        agora_monotonic = time.monotonic()
        fazer_descoberta = agora_monotonic >= self.next_discovery
        candidatos = [self.url_g1, G1]
        if fazer_descoberta:
            try:
                candidatos = descobrir_urls_g1() + candidatos
            except Exception as exc:
                erro_descoberta = f'{type(exc).__name__}: {str(exc)[:160]}'
            self.next_discovery = agora_monotonic + INTERVALO_DESCOBERTA
            self.discovery_at = time.time()
        candidatos = list(dict.fromkeys(url for url in candidatos if _url_g1_valida(url)))[:8]
        leituras = []
        try:
            erros = []
            for url in candidatos:
                try:
                    resumo, resolvida, modo = obter_g1(url)
                    leituras.append((resumo, resolvida, modo))
                except Exception as exc:
                    erros.append(f'{url}: {str(exc)[:120]}')
            if not leituras:
                raise ValueError(' | '.join(erros) or 'Nenhum endereço candidato foi encontrado')
            novo_g1, nova_url, novo_modo = max(
                leituras,
                key=lambda item: (_data_iso(item[0].get('atualizado_em')),
                                  item[0].get('lula', 0) + item[0].get('flavio', 0)),
            )
        except Exception as exc:
            erro_g1 = f'{type(exc).__name__}: {str(exc)[:240]}'
        try:
            novo_poder = parse_poder360(_baixar(PODER360))
        except Exception as exc:
            erro_poder = f'{type(exc).__name__}: {str(exc)[:160]}'
        with self.lock:
            antes = self.resumo_g1
            if novo_g1 is not None:
                self.resumo_g1 = novo_g1
                self.url_g1 = nova_url
                self.modo_g1 = novo_modo
                self.g1_online = True
                self.checked_at = time.time()
                self.origem_g1 = 'artigo_g1_descoberto_e_consultado' if nova_url != G1 else 'artigo_g1_consultado'
                if novo_g1 != antes:
                    self.changed_at = self.checked_at
            else:
                self.g1_online = False
            if novo_poder is not None:
                self.dados_poder = novo_poder
                self.poder_online = True
            else:
                self.poder_online = False
            self.erro_g1 = erro_g1
            self.erro_descoberta = erro_descoberta
            self.erro_poder = erro_poder
            self.buscando = False
            try:
                CACHE.write_text(json.dumps(dict(
                    url_g1=self.url_g1,
                    modo_g1=self.modo_g1,
                    url_poder360=PODER360,
                    resumo_g1=self.resumo_g1,
                    dados_poder=self.dados_poder,
                    alteracao_observada_em=self.changed_at,
                ), ensure_ascii=False), encoding='utf8')
            except OSError:
                pass

    def panorama_sem_lock(self):
        return dict(
            tipo='levantamento_jornalistico_nao_oficial',
            fonte=FONTE,
            url_fonte=self.url_g1,
            url_g1=self.url_g1,
            url_poder360=PODER360,
            resumo_g1=self.resumo_g1,
            dados_publicados_a_partir_de=PODER_PUBLICADO_EM,
            consulta_realizada_em=self.checked_at,
            alteracao_observada_em=self.changed_at,
            acesso_pagina_nesta_tentativa=self.g1_online,
            acesso_poder360_nesta_tentativa=self.poder_online,
            origem=self.origem_g1,
            modo_leitura_g1=self.modo_g1,
            descoberta_automatica=True,
            descoberta_realizada_em=self.discovery_at,
            dados_g1_desatualizados=not self.g1_online,
            erro_ultima_consulta=self.erro_g1,
            erro_descoberta_g1=self.erro_descoberta,
            erro_poder360=self.erro_poder,
            proxima_verificacao_segundos=self.refresh,
            paises_poder=rows(self.dados_poder),
            paises=rows(self.dados_poder),
            aviso=('Resumo principal lido do G1. A tabela por país é uma referência secundária do Poder360. '
                   'Não somar nenhuma das fontes jornalísticas aos BUs ou à totalização oficial do TSE.'),
        )

    def panorama(self):
        with self.lock:
            return self.panorama_sem_lock()

#!/usr/bin/env python3
"""Coletor conservador de BU do exterior 2026, por EA16 -> EA18 -> ASN.1.

Dados exibidos são PARCIAIS INDEPENDENTES por seção; não representam
projeção, resultado oficial nacional nem totalização ZZ.
"""
from __future__ import annotations

from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import unicodedata

PLEITO = 3221
ELEICAO = 6258
URL_RAIZ = 'https://resultados.tse.jus.br/oficial/ele2026/arquivo-urna/3221'
URL_EA16 = URL_RAIZ + '/config/zz/zz-p003221-cs.json'
# Adaptada por terceiros a partir de BU reais do primeiro turno de 2026.
# Baixar como DADOS de especificação ASN.1, sem executar código remoto.
URL_ASN1 = 'https://raw.githubusercontent.com/luqmarqs/dashboard-eleicoes-2026/main/src/tse2026/asn1/bu_2026.asn1'
DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('PAINEL_DATA_DIR', str(DIR))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / '.bu_exterior.sqlite3'
SPEC_PATH = DATA_DIR / 'bu_2026.asn1'

# Relação cidade/jurisdição conhecida; nomes não reconhecidos são mostrados
# como localidade, jamais atribuídos a um país por aproximação.
PAISES = {
 'tóquio':'Japão','toquio':'Japão','nagoya':'Japão','hamamatsu':'Japão','hiroshima':'Japão','osaka':'Japão',
 'pequim':'China','beijing':'China','xangai':'China','shanghai':'China','hong kong':'Hong Kong','taipé':'Taiwan','taipei':'Taiwan','taipê':'Taiwan','taipe':'Taiwan',
 'wellington':'Nova Zelândia','auckland':'Nova Zelândia','christchurch':'Nova Zelândia',
 'sydney':'Austrália','sidney':'Austrália','melbourne':'Austrália','brisbane':'Austrália','perth':'Austrália','canberra':'Austrália',
 'manila':'Filipinas','singapura':'Singapura','kuala lumpur':'Malásia','seul':'Coreia do Sul',
 'bangkok':'Tailândia','jacarta':'Indonésia','nova deli':'Índia','nova déli':'Índia','new delhi':'Índia','mumbai':'Índia',
 'lisboa':'Portugal','porto':'Portugal','faro':'Portugal','coimbra':'Portugal',
 'madri':'Espanha','madrid':'Espanha','barcelona':'Espanha','valencia':'Espanha','málaga':'Espanha',
 'londres':'Reino Unido','london':'Reino Unido','manchester':'Reino Unido','edimburgo':'Reino Unido',
 'paris':'França','lyon':'França','marselha':'França','bordeaux':'França',
 'berlim':'Alemanha','berlin':'Alemanha','frankfurt':'Alemanha','munique':'Alemanha','munich':'Alemanha','hamburgo':'Alemanha',
 'roma':'Itália','milão':'Itália','milao':'Itália','nápoles':'Itália','napoles':'Itália','turim':'Itália','florença':'Itália',
 'zurique':'Suíça','zurich':'Suíça','genebra':'Suíça','geneva':'Suíça','berna':'Suíça',
 'dublin':'Irlanda','amsterdã':'Países Baixos','amsterdam':'Países Baixos','bruxelas':'Bélgica','brussels':'Bélgica',
 'estocolmo':'Suécia','oslo':'Noruega','copenhague':'Dinamarca','helsinque':'Finlândia',
 'viena':'Áustria','praga':'Tchéquia','budapeste':'Hungria','varsóvia':'Polônia','moscou':'Rússia',
 'washington':'Estados Unidos','nova york':'Estados Unidos','new york':'Estados Unidos','miami':'Estados Unidos',
 'boston':'Estados Unidos','los angeles':'Estados Unidos','san francisco':'Estados Unidos',
 'chicago':'Estados Unidos','houston':'Estados Unidos','orlando':'Estados Unidos','atlanta':'Estados Unidos',
 'seattle':'Estados Unidos','dallas':'Estados Unidos','filadélfia':'Estados Unidos','newark':'Estados Unidos',
 'toronto':'Canadá','montreal':'Canadá','vancouver':'Canadá','ottawa':'Canadá',
 'buenos aires':'Argentina','córdoba':'Argentina','cordoba':'Argentina','mendoza':'Argentina',
 'montevidéu':'Uruguai','montevideo':'Uruguai','assunção':'Paraguai','asuncion':'Paraguai',
 'santiago':'Chile','valparaíso':'Chile','lima':'Peru','quito':'Equador','guayaquil':'Equador',
 'bogotá':'Colômbia','bogota':'Colômbia','caracas':'Venezuela','la paz':'Bolívia','santa cruz de la sierra':'Bolívia',
 'cidade do méxico':'México','méxico':'México','mexico':'México','cancún':'México',
 'panamá':'Panamá','cidade do panamá':'Panamá','são josé':'Costa Rica','san josé':'Costa Rica',
 'tel aviv':'Israel','jerusalém':'Israel','dubai':'Emirados Árabes Unidos','abu dhabi':'Emirados Árabes Unidos',
 'doha':'Catar','riade':'Arábia Saudita','rabat':'Marrocos','cairo':'Egito','luanda':'Angola',
 'maputo':'Moçambique','pretória':'África do Sul','cidade do cabo':'África do Sul','joanesburgo':'África do Sul'
}

# Prioridade de descoberta: locais cujo dia eleitoral termina primeiro por fuso.
# A origem continua sendo somente o TSE; o painel não faz inferência de votos.
PAISES_ANTECIPADOS = {
    'Nova Zelândia': 0, 'Austrália': 1, 'Japão': 2, 'Coreia do Sul': 3,
    'China': 4, 'Hong Kong': 5, 'Taiwan': 6, 'Singapura': 7,
    'Filipinas': 8, 'Malásia': 9, 'Indonésia': 10, 'Tailândia': 11, 'Índia': 12,
}

def pais_da_cidade(cidade):
    chave=str(cidade).strip().lower()
    if chave in PAISES: return PAISES[chave]
    chave=unicodedata.normalize('NFKD',chave)
    chave=''.join(x for x in chave if not unicodedata.combining(x))
    for nome,pais in PAISES.items():
        normal=unicodedata.normalize('NFKD',nome)
        normal=''.join(x for x in normal if not unicodedata.combining(x))
        if chave==normal: return pais
    return None

def inteiro(valor):
    try: return int(valor)
    except (ValueError, TypeError): return 0

def requisitar(url, limite=3_000_000):
    req = urllib.request.Request(url, headers={'User-Agent':'PainelEleitoralIndependente/1.0', 'Accept':'application/json,application/octet-stream,*/*'})
    with urllib.request.urlopen(req, timeout=13) as r:
        corpo=r.read(limite+1)
        if len(corpo)>limite: raise ValueError('Arquivo maior que o limite de segurança')
        return corpo

def obter_json(url):
    obj=json.loads(requisitar(url).decode('utf-8-sig'))
    if not isinstance(obj,dict): raise ValueError('JSON incompatível')
    return obj

def montar_arquivo_auxiliar(m,z,s):
    base=f'{URL_RAIZ}/dados/zz/{m}/{z}/{s}'
    arq=f'p003221-zz-m{m}-z{z}-s{s}-aux.json'
    return base, f'{base}/{arq}'

def inventariar(obj):
    if inteiro(obj.get('cdp'))!=PLEITO: raise ValueError('EA16 de pleito incorreto')
    if obj.get('f')!='o': raise ValueError('EA16 não está em fase oficial')
    out=[]
    for abr in obj.get('abr',[]):
        if str(abr.get('cd','')).lower()!='zz': continue
        for mu in abr.get('mu',[]):
            cod=str(mu.get('cd','')).zfill(5)
            if not re.fullmatch(r'\d{5}',cod): continue
            cidade=str(mu.get('nm','Localidade')).strip()
            for zon in mu.get('zon',[]):
                zona=str(zon.get('cd','')).zfill(4)
                if not re.fullmatch(r'\d{4}',zona): continue
                for sec in zon.get('sec',[]):
                    ns=str(sec.get('ns','')).zfill(4)
                    nsp=str(sec.get('nsp',ns)).zfill(4)
                    if not re.fullmatch(r'\d{4}',ns): continue
                    # Apenas as seções principais têm BU: agregadas já estão nele.
                    if nsp!=ns: continue
                    if not (str(sec.get('da','')).strip() and str(sec.get('ha','')).strip()): continue
                    out.append(dict(key=f'{cod}:{zona}:{ns}',cidade=cidade,mun=cod,zona=zona,secao=ns,geracao=f"{sec['da']} {sec['ha']}"))
    return out

def seleciona_hash(aux):
    hashes=aux.get('hashes',[])
    if not isinstance(hashes,list): return None
    # Nunca misturar BUs de hashes diferentes. Preferir o hash totalizado.
    validos=[]
    for item in hashes:
        h=str(item.get('hash','')).strip()
        if not re.fullmatch(r'[a-zA-Z0-9_+=-]{16,128}',h): continue
        status=str(item.get('st','')).lower()
        if any(token in status for token in ('exclu','cancel','rejeit','substitu','anulad','invalid')):
            continue
        for arq in item.get('arq',[]):
            nome=arq.get('nm','')
            tipo=arq.get('tp','')
            if (tipo in ('bu','busa') or nome.endswith(('-bu.dat','-busa.dat'))) and re.fullmatch(r'[a-zA-Z0-9_.-]{10,150}',nome):
                validos.append((item, h, nome))
                break
    if not validos: return None
    preferidos=[a for a in validos if str(a[0].get('st','')).lower() in ('totalizado','totalizada')]
    recentes=preferidos or validos
    def data_hora(x):
        i=x[0]
        dt=str(i.get('dr',''))
        hr=str(i.get('hr',''))
        return (dt[6:10]+dt[3:5]+dt[:2]+hr) if len(dt)==10 else (dt+hr)
    item,h,nome=max(recentes,key=data_hora)
    return h,nome,str(item.get('st','')).strip()

class BUParser:
    def __init__(self):
        self._spec=None
        self._preparar_lock=threading.Lock()

    def preparar(self):
        with self._preparar_lock:
            self._preparar_sem_concorrencia()

    def _preparar_sem_concorrencia(self):
        if self._spec is not None: return
        try: import asn1tools
        except ImportError as exc: raise RuntimeError('Dependência asn1tools não instalada; execute Preparar_Coletor_Mac.command') from exc
        if not SPEC_PATH.exists():
            raw=requisitar(URL_ASN1,limite=120_000)
            if b'EntidadeBoletimUrna' not in raw: raise ValueError('Especificação ASN.1 incompatível')
            SPEC_PATH.write_bytes(raw)
        self._spec=asn1tools.compile_files(str(SPEC_PATH),codec='ber')

    def ler(self,raw,esperado):
        self.preparar()
        envelope=self._spec.decode('EntidadeEnvelopeGenerico',raw)
        if envelope.get('tipoEnvelope')!='envelopeBoletimUrna': raise ValueError('Arquivo não é envelope de BU')
        if envelope.get('seguranca') is not None: raise ValueError('BU cifrado não suportado')
        bu=self._spec.decode('EntidadeBoletimUrna',envelope['conteudo'])
        iden=bu['identificacaoSecao']
        munzona=iden['municipioZona']
        if (inteiro(munzona['municipio']),inteiro(munzona['zona']),inteiro(iden['secao'])) != (int(esperado['mun']),int(esperado['zona']),int(esperado['secao'])):
            raise ValueError('BU pertence a outra seção')
        if bu.get('fase') not in ('oficial','o'):
            raise ValueError('BU não identificado como oficial')
        id_eleitoral=bu.get('cabecalho',{}).get('idEleitoral')
        if id_eleitoral and id_eleitoral[0]=='idPleito' and inteiro(id_eleitoral[1])!=PLEITO:
            raise ValueError('BU pertence a outro pleito')
        votos=defaultdict(int)
        branco=nulo=0
        comparecimento=None
        cargos=0
        for el in bu.get('resultadosVotacaoPorEleicao',[]):
            if inteiro(el.get('idEleicao'))!=ELEICAO: continue
            for rv in el.get('resultadosVotacao',[]):
                for cargo in rv.get('totaisVotosCargo',[]):
                    cd=cargo.get('codigoCargo')
                    if not (isinstance(cd,tuple) and cd==('cargoConstitucional','presidente')): continue
                    cargos+=1
                    if cargos>1: raise ValueError('Duplicidade inesperada do cargo de Presidente no BU')
                    comparecimento=inteiro(rv.get('qtdComparecimento'))
                    for v in cargo.get('votosVotaveis',[]):
                        qtd=inteiro(v.get('quantidadeVotos'))
                        if qtd<0: raise ValueError('Votação negativa')
                        tipo=v.get('tipoVoto')
                        if tipo=='nominal':
                            ident=v.get('identificacaoVotavel') or {}
                            numero=inteiro(ident.get('codigo'))
                            if not 1<=numero<=99: raise ValueError('Número de candidato inválido')
                            votos[str(numero)]+=qtd
                        elif tipo=='branco': branco+=qtd
                        elif tipo=='nulo': nulo+=qtd
                        elif qtd>0: raise ValueError('Tipo de voto inesperado em cargo de Presidente: '+str(tipo))
        if cargos!=1 or comparecimento is None: raise ValueError('BU sem resultado presidencial do segundo turno')
        validos=sum(votos.values())
        if comparecimento!=validos+branco+nulo: raise ValueError('Soma de votos diverge do comparecimento do BU')
        return dict(votos=dict(votos),validos=validos,brancos=branco,nulos=nulo,comparecimento=comparecimento)

class Coletor:
    def __init__(self, db_path=DB_PATH, fetch_json=obter_json, fetch_bytes=requisitar, parser=None):
        self.lock=threading.RLock()
        self.fetch_json=fetch_json
        self.fetch_bytes=fetch_bytes
        self.parser=parser or BUParser()
        self.db=sqlite3.connect(str(db_path),check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS urnas (chave TEXT PRIMARY KEY, cidade TEXT NOT NULL,
           municipio TEXT,zona TEXT,secao TEXT, hash TEXT NOT NULL, situacao TEXT, dados TEXT NOT NULL,
           geracao TEXT, atualizado REAL NOT NULL)''')
        self.db.commit()
        self.tentativas={}
        self.proximo_ea16=0
        self.inventario=[]
        self.total_principais=0
        self.atualizacao=0
        self.ultimo_sucesso=0
        self.erro=None
        self.etapa='Inicializando'
        self.stop=threading.Event()
        self.falhas=0
        self.thread=None
        self.contadores=Counter()
        self.ultimos_erros=[]
        self.amostras_aux=[]
        self.status_aux=Counter()
        self.secoes_pendentes=0
        self.secoes_tentadas_ciclo=0

    def iniciar(self):
        if self.thread is not None: return
        self.thread=threading.Thread(target=self.loop,name='coletor-bu-exterior',daemon=True)
        self.thread.start()

    def _aguardar(self,seg): self.stop.wait(seg)

    def loop(self):
        while not self.stop.is_set():
            try:
                self.ciclo()
                self.falhas=0
                self._aguardar(45)
            except urllib.error.HTTPError as err:
                self.erro=f'TSE HTTP {err.code} ao consultar BU: aguardando nova tentativa'
                self.etapa='Consulta temporariamente indisponível'
                self.falhas+=1
                self._aguardar(660 if err.code in (403,429) else min(300,30*self.falhas))
            except Exception as err:
                self.erro=f'{type(err).__name__}: {str(err)[:180]}'
                self.etapa='Coleta interrompida temporariamente'
                self.falhas+=1
                self._aguardar(min(240,30*self.falhas))

    def _registrar_erro(self,etapa,secao,erro):
        mensagem=f'{etapa} {secao}: {str(erro)[:140]}'
        with self.lock:
            self.contadores['erros']+=1
            self.ultimos_erros=(self.ultimos_erros+[mensagem])[-8:]
            self.erro=mensagem

    def _uma_secao(self,se):
        chave=se['key']
        with self.lock:
            salvo=self.db.execute('SELECT hash,geracao FROM urnas WHERE chave=?',(chave,)).fetchone()
        if salvo and salvo[1]==se['geracao']:
            return 'cache'
        base,url=montar_arquivo_auxiliar(se['mun'],se['zona'],se['secao'])
        try:
            aux=self.fetch_json(url)
            hashes=aux.get('hashes')
            total_hashes=len(hashes) if isinstance(hashes,list) else 0
            tipos=Counter()
            situacoes=Counter()
            for item in hashes if isinstance(hashes,list) else []:
                situacoes[str(item.get('st') or '(sem status)')]+=1
                for arq in (item.get('arq') or []):
                    tipos[str(arq.get('tp') or '(sem tipo)')]+=1
            versao=seleciona_hash(aux)
            situacao_aux=str(aux.get('st') or '(sem status)')
            with self.lock:
                self.contadores['aux_lidos']+=1
                self.status_aux[situacao_aux]+=1
                self.contadores['aux_com_hashes' if total_hashes else 'aux_sem_hashes']+=1
                if tipos.get('bu',0) or tipos.get('busa',0):self.contadores['aux_com_arquivo_bu']+=1
                # Amostras limitadas para inspecionar a estrutura real sem expor arquivos inteiros.
                # Registra localidades diferentes primeiro.
                if not any(a['cidade']==se['cidade'] for a in self.amostras_aux) and len(self.amostras_aux)<24:
                    self.amostras_aux.append(dict(cidade=se['cidade'],secao=chave,
                      situacao_aux=situacao_aux,hashes=total_hashes,
                      situacoes=dict(situacoes),tipos=dict(tipos),selecionado=bool(versao),
                      chaves_aux=sorted(aux.keys())))
            if aux.get('f') not in (None,'o'):
                with self.lock:self.contadores['aux_fase_invalida']+=1
                return 'fase'
            if not versao:
                with self.lock:self.contadores['aux_sem_bu_utilizavel']+=1
                return 'sem_bu'
            with self.lock:self.contadores['bu_selecionado']+=1
            hash_id,arquivo,situacao=versao
            if salvo and salvo[0]==hash_id:
                # Atualiza apenas o carimbo se a seção continua no mesmo hash.
                with self.lock:
                    self.db.execute('UPDATE urnas SET geracao=? WHERE chave=?',(se['geracao'],chave))
                    self.db.commit()
                return 'mesmo_hash'
            raw=self.fetch_bytes(f'{base}/{hash_id}/{arquivo}')
            with self.lock:self.contadores['bu_baixado']+=1
            dados=self.parser.ler(raw,se)
            with self.lock:self.contadores['bu_decodificado']+=1
            with self.lock:
                self.db.execute("INSERT OR REPLACE INTO urnas (chave,cidade,municipio,zona,secao,hash,situacao,dados,geracao,atualizado) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (chave,se['cidade'],se['mun'],se['zona'],se['secao'],hash_id,situacao,
                     json.dumps(dados,separators=(',',':')),se['geracao'],time.time()))
                self.db.commit()
                self.ultimo_sucesso=time.time()
                self.contadores['novos_bu']+=1
            return 'novo'
        except urllib.error.HTTPError as err:
            if err.code in (403,429):
                self._registrar_erro('limite/acesso TSE',chave,f'HTTP {err.code}')
                raise
            self._registrar_erro('HTTP',chave,f'HTTP {err.code}')
            return 'nao_encontrado' if err.code==404 else 'erro_http'
        except Exception as err:
            self._registrar_erro('BU/AUX',chave,f'{type(err).__name__}: {err}')
            if 'asn1tools' in str(err):
                raise
            return 'erro'

    def ciclo(self):
        agora=time.time()
        if not self.inventario or agora>=self.proximo_ea16:
            self.etapa='Lendo cadastro de seções do TSE (EA16)'
            conf=self.fetch_json(URL_EA16)
            inventario=inventariar(conf)
            # Mapeamento por fuso: primeiro países com urnas encerradas; depois
            # outras localidades, por ordem de liberação do próprio TSE.
            inventario.sort(key=lambda x:(
                PAISES_ANTECIPADOS.get(pais_da_cidade(x['cidade']),100),
                str(x['geracao']),x['cidade'],x['key']))
            with self.lock:
                self.inventario=inventario
                self.total_principais=len(inventario)
                self.contadores['ea16_leituras']+=1
                self.proximo_ea16=time.time()+90
                self.atualizacao=time.time()
                self.erro=None
            if not inventario:
                self.etapa='EA16 sem seção principal com data de recebimento'
                return
        self.etapa='Lendo BUs disponíveis (Ásia e Oceania prioritárias)'
        # Evita ficar aguardando centenas de cidades antes de ler os BUs
        # nos locais que encerraram cedo. O lote pode ser reduzido em caso de 429.
        lote=[]
        for se in self.inventario:
            if self.stop.is_set(): break
            with self.lock:
                salvo=self.db.execute('SELECT geracao FROM urnas WHERE chave=?',(se['key'],)).fetchone()
            if salvo and salvo[0]==se['geracao']: continue
            if agora-self.tentativas.get(se['key'],0)<180: continue
            lote.append(se)
            self.tentativas[se['key']]=agora
            if len(lote)>=90: break
        with self.lock:
            self.secoes_pendentes=max(0,len(self.inventario)-self.db.execute('SELECT COUNT(*) FROM urnas').fetchone()[0])
            self.secoes_tentadas_ciclo=len(lote)
            self.contadores['tentativas_aux']+=len(lote)
        if lote:
            # 5 conexões simultâneas (muito abaixo do limite da CDN).
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures=[pool.submit(self._uma_secao,se) for se in lote]
                for future in as_completed(futures):
                    try: future.result()
                    except urllib.error.HTTPError as err:
                        if err.code in (403,429):
                            self.etapa='Limite do TSE: aguardando para não bloquear o IP'
                            raise
                    except RuntimeError as err:
                        if 'asn1tools' in str(err):
                            self.etapa='Leitor ASN.1 não instalado no Mac'
                            raise
        with self.lock:
            self.atualizacao=time.time()
        self.etapa='Monitorando novos BUs'

    def panorama(self):
        with self.lock:
            registros=self.db.execute('SELECT cidade,municipio,zona,secao,situacao,dados,atualizado FROM urnas').fetchall()
            nome_etapa=self.etapa; erro=self.erro; last=self.ultimo_sucesso; total=self.total_principais; leitura=self.atualizacao
            diagnostico=dict(self.contadores); exemplos=list(self.ultimos_erros); pendentes=self.secoes_pendentes; lote=self.secoes_tentadas_ciclo
            amostras_aux=list(self.amostras_aux); status_aux=dict(self.status_aux)
        por_cidade=defaultdict(lambda:dict(secoes=0,votos=defaultdict(int),brancos=0,nulos=0,validos=0,comparecimento=0))
        votos=defaultdict(int); branco=nulo=comparecimento=0
        ultimo=0
        for cidade,mun,z,s,situacao,dado,quando in registros:
            x=json.loads(dado); ultimo=max(ultimo,quando)
            item=por_cidade[(cidade,mun)]; item['secoes']+=1
            for numero,qt in x['votos'].items(): item['votos'][numero]+=qt; votos[numero]+=qt
            for chave in ('validos','brancos','nulos','comparecimento'): item[chave]+=x[chave]
            branco+=x['brancos'];nulo+=x['nulos'];comparecimento+=x['comparecimento']
        def empacotar(v):
            total=sum(v.values())
            return [dict(numero=k,votos=num,percentual=round(num*100/total,2) if total else 0)
                    for k,num in sorted(v.items(),key=lambda p:(-p[1],p[0]))]
        locais=[]
        por_pais=defaultdict(lambda:dict(secoes=0,votos=defaultdict(int),validos=0,comparecimento=0,cidades=0))
        for (cidade,mun),dados in por_cidade.items():
            pais=pais_da_cidade(cidade)
            locais.append(dict(cidade=cidade,codigo=mun,pais=pais,
                               secoes=dados['secoes'],validos=dados['validos'],votos=empacotar(dados['votos']),
                               comparecimento=dados['comparecimento']))
            if pais:
                ag=por_pais[pais]
                ag['secoes']+=dados['secoes'];ag['validos']+=dados['validos'];ag['comparecimento']+=dados['comparecimento'];ag['cidades']+=1
                for numero,qtd in dados['votos'].items():ag['votos'][numero]+=qtd
        if not registros and diagnostico.get('aux_lidos',0)>0:
            if diagnostico.get('bu_baixado',0)>0:
                nome_etapa='Boletins baixados, mas nenhum validado: conferir erros de decodificação'
            elif diagnostico.get('bu_selecionado',0)>0:
                nome_etapa='Arquivo BU indicado pelo TSE, mas ainda não baixado'
            elif diagnostico.get('aux_sem_hashes',0)==diagnostico.get('aux_lidos',0):
                nome_etapa='Auxiliares consultados sem hash de boletim disponível'
            else:
                nome_etapa='Auxiliares consultados, nenhum BU utilizável identificado'
        locais.sort(key=lambda x:(-x['validos'],x['cidade']))
        paises=[dict(pais=nome,secoes=v['secoes'],validos=v['validos'],cidades=v['cidades'],
                    votos=empacotar(v['votos'])) for nome,v in por_pais.items()]
        paises.sort(key=lambda x:(-x['validos'],x['pais']))
        return dict(tipo='previa_bu_independente',fonte='Arquivos de urna do TSE (EA16/EA18/BU)',
                    status=nome_etapa,erro=erro,sec_principais_publicadas=len(registros),
                    sec_principais_identificadas=total,total_votos_validos=sum(votos.values()),
                    brancos=branco,nulos=nulo,comparecimento=comparecimento,atualizado_em=last or None,
                    checado_em=leitura or None,diagnostico=diagnostico,ultimos_erros=exemplos,
                    amostras_aux=amostras_aux, situacoes_aux=status_aux,
                    secoes_pendentes=pendentes,ultimo_lote=lote,candidatos=empacotar(votos),paises=paises,localidades=locais,
                    aviso='Parcial independente por BUs recebidos, NÃO é totalização oficial nem previsão de resultado. Seções agregadas contam na principal.')

if __name__=='__main__':
    c=Coletor(); print(json.dumps(c.panorama(),ensure_ascii=False,indent=2))

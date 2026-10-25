#!/usr/bin/env python3
"""Painel independente: proxy/cache dos JSON públicos oficiais do TSE (eleições 2026, 2º turno)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from coletor_bu import Coletor
from imprensa_exterior import Imprensa
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8765"))
WEB_MODE = os.environ.get("WEB_MODE", "0").lower() in {"1", "true", "yes"}
HOST = "0.0.0.0" if WEB_MODE else "127.0.0.1"
ENABLE_BU_COLLECTOR = os.environ.get("ENABLE_BU_COLLECTOR", "0" if WEB_MODE else "1").lower() in {"1", "true", "yes"}
UF_VALIDAS = {"br", "ac", "al", "ap", "am", "ba", "ce", "df", "es", "go", "ma", "mt", "ms", "mg", "pa", "pb", "pr", "pe", "pi", "rj", "rn", "rs", "ro", "rr", "sc", "sp", "se", "to", "zz"}
INTERVALO = 20  # Segundos mínimos entre solicitações de uma mesma UF à origem
CACHE: dict[str, dict] = {}
LOCK = threading.RLock()
COLETOR = Coletor() if ENABLE_BU_COLLECTOR else None
IMPRENSA = Imprensa()


def consultar(uf: str):
    agora = time.monotonic()
    with LOCK:
        anterior = CACHE.get(uf)
        if anterior and agora - anterior.get("tentativa", 0) < INTERVALO:
            return anterior
        # Reserva a janela de busca para evitar múltiplos pedidos concorrentes da mesma UF.
        estado = dict(anterior or {})
        estado["tentativa"] = agora
        CACHE[uf] = estado

    caminho = f"{uf}/{uf}-c0001-e006258-u.json"
    url = "https://resultados.tse.jus.br/oficial/ele2026/6258/dados/" + caminho
    headers = {"User-Agent": "PainelResultadosTSE2026/1.0", "Accept": "application/json"}
    if estado.get("etag"):
        headers["If-None-Match"] = estado["etag"]
    if estado.get("modified"):
        headers["If-Modified-Since"] = estado["modified"]
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            obj = json.loads(resp.read(2_500_000).decode("utf-8-sig"))
            if not isinstance(obj, dict) or "carg" not in obj or "s" not in obj:
                raise ValueError("Formato dos dados inesperado")
            recebido = {"dados": obj, "erro": None, "etag": resp.headers.get("ETag"),
                        "modified": resp.headers.get("Last-Modified"), "obtido": time.time()}
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and estado.get("dados"):
            recebido = {"erro": None, "obtido": time.time()}
        elif exc.code == 404:
            recebido = {"erro": "O arquivo oficial desta abrangência ainda não está disponível (HTTP 404)."}
        else:
            recebido = {"erro": f"O TSE respondeu com HTTP {exc.code}."}
    except Exception as exc:
        recebido = {"erro": f"Falha ao consultar o TSE: {type(exc).__name__}: {exc}"}

    with LOCK:
        atual = CACHE.get(uf, {})
        # Pode haver outra consulta paralela; mantém a última resposta recebida e dados anteriores.
        atual.update(recebido)
        CACHE[uf] = atual
        return dict(atual)


class Handler(BaseHTTPRequestHandler):
    def responder(self, body: bytes, status: int = 200, content_type: str = "application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html = (BASE / "index.html").read_bytes()
            return self.responder(html, content_type="text/html; charset=utf-8")
        if parsed.path == "/health":
            return self.responder(b'{"status":"ok","servico":"painel_tse_2026"}')
        if parsed.path == "/api/imprensa-exterior":
            payload = IMPRENSA.atualizar()
            return self.responder(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if parsed.path == "/api/bu-exterior":
            if COLETOR is None:
                payload = {
                    "status": "Coletor de BU desativado nesta hospedagem",
                    "erro": "Coleta independente de BUs não habilitada neste servidor.",
                    "total_votos_validos": 0,
                    "sec_principais_publicadas": 0,
                    "sec_principais_identificadas": 0,
                    "comparecimento": 0,
                    "candidatos": [],
                    "localidades": [],
                    "diagnostico": {},
                }
            else:
                payload = COLETOR.panorama()
            return self.responder(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if parsed.path == "/api/resultados":
            uf = parse_qs(parsed.query).get("uf", ["br"])[0].lower()
            if uf not in UF_VALIDAS:
                return self.responder(b'{"erro":"UF invalida"}', 400)
            resultado = consultar(uf)
            payload = {
                "uf": uf.upper(), "dados": resultado.get("dados"), "erro": resultado.get("erro"),
                "recebido_em": resultado.get("obtido"), "fonte": "Tribunal Superior Eleitoral (TSE)",
                "url_oficial": f"https://resultados.tse.jus.br/oficial/ele2026/6258/dados/{uf}/{uf}-c0001-e006258-u.json",
                "intervalo_segundos": INTERVALO,
            }
            return self.responder(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 200 if payload["dados"] else 503)
        if parsed.path == "/favicon.ico":
            return self.responder(b"", status=204)
        return self.responder(b"Nao encontrado", 404, "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def main():
    print("=" * 61)
    print("PAINEL ELEITORAL AO VIVO — 2o TURNO, 25/10/2026")
    print("Fonte: dados publicos do Tribunal Superior Eleitoral")
    print("O painel NÃO é oficial do TSE.")
    print("=" * 61)
    if COLETOR is not None:
        COLETOR.iniciar()
        print("Coletor de boletins do exterior habilitado.", flush=True)
    else:
        print("Coletor de boletins desativado; totalizacoes e imprensa continuam disponiveis.", flush=True)
    try:
        servidor = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"Nao foi possivel abrir a porta {PORT}: {exc}")
        sys.exit(1)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Abra {url} se o navegador nao iniciar automaticamente.")
    print("Servidor iniciado. Ctrl+C para encerrar.\n", flush=True)
    if not WEB_MODE:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()

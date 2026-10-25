# Painel eleitoral 2026 — publicação na internet (Render)

## Visão geral
A página HTML **depende do servidor Python** para `/api/resultados`, `/api/imprensa-exterior` e `/api/bu-exterior`. Publicar somente `index.html` em hospedagem estática faz as consultas falharem. Esta edição do projeto abre `0.0.0.0:$PORT`, oferece `/health`, e não abre navegador no servidor.

## Publicar
1. Crie um repositório novo (sugestão: `painel-eleicoes-2026`) em https://github.com/new. Envie os arquivos **na raiz** do repositório; não envie o ZIP como um único arquivo. Este pacote já exclui bancos `.sqlite3`, caches, binários Python e arquivos de máquina.
2. No https://dashboard.render.com/ clique em **New > Web Service** (não Static Site) e conecte o repositório. Alternativamente, importe o `render.yaml` como Blueprint.
3. Use:
   - Build: `pip install -r requirements.txt`
   - Start: `python server.py`
   - Variável `WEB_MODE=1`
   - Variável `ENABLE_BU_COLLECTOR=0` para começar (leve e seguro).
   - Health check: `/health`
4. Acesse o domínio `.onrender.com` exibido pelo serviço. O domínio pode ser personalizado.

## Alertas operacionais
- **Render Free** dorme após cerca de 15 min sem tráfego, podendo levar ~1 min para acordar. Seu sistema de arquivos é temporário: os bancos locais e caches são perdidos em reinícios/redeploys. Um coletor de BU contínuo não é adequado para esse plano.
- A URL do serviço é pública. O código faz muitas consultas ao TSE, especialmente para múltiplas UFs. Hospedagens podem impor limites de tráfego, CPU ou saída; monitore logs e reduza cadência se necessário.
- Se usar plano pago com disco persistente, defina `PAINEL_DATA_DIR` para o diretório montado pelo provedor e, após validar o desempenho, `ENABLE_BU_COLLECTOR=1`. **Isso não resolve automaticamente os BUs zerados** observados no Mac; pode exigir investigação do formato/publicação.
- O painel está rotulado como visualização independente. Não afirmar que representa o TSE.
- **Transparência da imprensa**: o resumo e a tabela por país são lidos diretamente da mesma matéria do G1. O painel só publica uma atualização quando a quantidade de países, a soma dos votos e o número de vitórias por candidato fecham entre si. O Poder360 permanece apenas como referência secundária identificada.
- Não somar resultados jornalísticos ao TSE nem aos BUs, para evitar dupla contagem.
- Fonte TSE: https://resultados.tse.jus.br/ ; G1: https://g1.globo.com/mundo/noticia/2026/10/25/votacao-no-exterior-confira-os-resultados-do-2o-turno-entre-lula-e-flavio-bolsonaro.ghtml
- Verifique as condições de uso e atribuição dos dados jornalísticos antes de divulgar publicamente a reprodução de trechos ou resultados.

## Teste local
```bash
WEB_MODE=1 PORT=8765 ENABLE_BU_COLLECTOR=0 python3 server.py
```
Depois visite http://localhost:8765/ e http://localhost:8765/health.

## Endpoints
- `/` — página principal
- `/health` — estado de saúde do servidor
- `/api/resultados?uf=br` — JSON TSE por UF (br, zz etc.)
- `/api/imprensa-exterior` — registros jornalísticos separados
- `/api/bu-exterior` — status da coleta independente

## Para publicação assistida pelo ChatGPT
A conexão com GitHub permite trabalhar nos repositórios acessíveis, mas este fluxo precisa de um repositório novo que você crie na sua conta. Depois de criá-lo, compartilhe apenas a URL do repositório e conecte a integração Render no ChatGPT. Não compartilhe senhas, tokens, cookies ou credenciais.

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import imprensa_exterior as ie


HTML_G1 = '''
<html><head><meta itemprop="dateModified" content="2026-10-25T16:23:26.988-03:00">
<link rel="canonical" href="https://g1.globo.com/mundo/noticia/2026/10/25/nova-materia.ghtml"></head>
<body><p>Lula - 15 votos Flávio Bolsonaro - 11 votos</p>
<p>Lula venceu em 1 países. Flávio Bolsonaro venceu em 1 países.</p>
<h2>OCEANIA E ÁSIA</h2>
<h2>Austrália</h2><ul><li>2º turno: Lula, 10 votos</li><li>2º turno: Flávio, 3 votos</li></ul>
<h2>Japão</h2><ul><li>2º turno: Flávio Bolsonaro, 8 votos</li><li>2º turno: Lula, 5 votos</li></ul>
<h3>Resumo do dia</h3></body></html>
'''


class ImprensaExteriorTests(unittest.TestCase):
    def test_parse_g1(self):
        resumo = ie.parse_g1(HTML_G1)
        self.assertEqual(resumo['lula'], 15)
        self.assertEqual(resumo['flavio'], 11)
        self.assertEqual(resumo['paises_territorios'], 2)
        paises = ie.parse_g1_paises(HTML_G1, resumo)
        self.assertEqual(paises, {'Austrália': (10, 3), 'Japão': (5, 8)})

    def test_descobre_novo_endereco_no_sitemap_oficial(self):
        novo = ('https://g1.globo.com/mundo/noticia/2026/10/25/'
                'votacao-no-exterior-resultados-2o-turno-lula-flavio.ghtml')
        indice = '<sitemapindex><sitemap><loc>https://g1.globo.com/sitemap/g1/2026/10/25_1.xml</loc></sitemap></sitemapindex>'
        diario = f'<urlset><url><loc>{novo}</loc></url></urlset>'

        def resposta(url, **_kwargs):
            return (indice if url == ie.G1_SITEMAP else diario), url

        with patch.object(ie, '_baixar_resposta', side_effect=resposta):
            urls = ie.descobrir_urls_g1(datetime(2026, 10, 25, 16, tzinfo=ZoneInfo('America/Sao_Paulo')))
        self.assertEqual(urls, [novo])

    def test_usa_amp_quando_pagina_normal_falha(self):
        def resposta(url, **_kwargs):
            if '/google/amp/' not in url:
                raise TimeoutError('página normal indisponível')
            return HTML_G1, url

        with patch.object(ie, '_baixar_resposta', side_effect=resposta):
            resumo, paises, resolvida, modo = ie.obter_g1(ie.G1)
        self.assertEqual(modo, 'pagina_amp')
        self.assertEqual(resumo['vitorias_lula'], 1)
        self.assertEqual(len(paises), 2)
        self.assertEqual(resolvida, 'https://g1.globo.com/mundo/noticia/2026/10/25/nova-materia.ghtml')

    def test_rejeita_tabela_que_nao_fecha_com_resumo(self):
        resumo = ie.parse_g1(HTML_G1.replace('Lula - 15 votos', 'Lula - 16 votos'))
        with self.assertRaisesRegex(ValueError, 'não fecha com o resumo'):
            ie.parse_g1_paises(HTML_G1, resumo)


if __name__ == '__main__':
    unittest.main()

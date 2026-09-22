#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_interdicoes.py
------------------------
Consolida interdições rodoviárias no Brasil a partir de fontes oficiais,
realiza a geocodificação de novos pontos encontrados por scraping e
regenera o arquivo de dados (interdicoes.json) consumido pelo mapa.

Uso:
    python3 atualizar_interdicoes.py
"""

import json
import re
import sys
import datetime
import urllib.request
import urllib.parse
import time
import pathlib

HERE = pathlib.Path(__file__).parent.resolve()
OUT = HERE / "interdicoes.json"
MANUAL = HERE / "interdicoes_manual.json"

# ---- Fontes oficiais (best-effort) -----------------------------------------
SOURCES = {
    "DER-SP / Defesa Civil SP": "https://www.der.sp.gov.br",
    "Secom/DAER-RS": "https://www.estado.rs.gov.br/rodovias-estaduais-apresentam-bloqueios-totais-ou-parciais-em-virtude-das-chuvas",
    "Defesa Civil RS": "https://prepara.rs.gov.br/elnino",
    "DER-MG": "https://observatorio.infraestrutura.mg.gov.br",
    "DNIT": "https://www.dnit.gov.br",
    "PRF": "https://www.gov.br/prf/pt-br",
}

# Regex para captura de rodovias e quilometragens
RE_RODOVIA = re.compile(
    r'\b((?:BR|SP|ERS|VRS|MG|PR|RJ|BA|SC|RS|GO|MT|MS|PA|PE|CE|MA|AL|SE|PB|RN|PI|TO|AM|AC|RO|RR|AP|ES|DF)[- ]?\d{2,4})\b'
)
RE_KM = re.compile(r'km\s*([\d.,]+(?:\s*(?:a|ao|e)\s*[\d.,]+)?)', re.I)

# Cache em memória para evitar requisições repetidas ao geocodificador
GEO_CACHE = {}

def fetch(url):
    """Realiza requisição HTTP com headers simulando um navegador real."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")

def geocodificar_local(local, uf):
    """
    Tenta obter lat/lon via Nominatim (OpenStreetMap) gratuito caso o item automático não possua coordenadas.
    """
    if not local or len(local.strip()) < 3:
        return None, None

    query = f"{local}, {uf}, Brasil"
    if query in GEO_CACHE:
        return GEO_CACHE[query]

    try:
        url = f"https://nominatim.openstreetmap.org/search?format=json&q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "PainelRodoviasBrasil/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode('utf-8'))
            if res:
                lat = float(res[0]['lat'])
                lon = float(res[0]['lon'])
                GEO_CACHE[query] = (lat, lon)
                time.sleep(1) # Respeita o rate limit do Nominatim (1s por request)
                return lat, lon
    except Exception as e:
        print(f"  [geocoding info] Falha ao geocodificar '{query}': {e}", file=sys.stderr)

    GEO_CACHE[query] = (None, None)
    return None, None

def scrape_best_effort():
    """Varre as páginas oficiais buscando menções de bloqueios em rodovias."""
    achados = []
    for fonte, url in SOURCES.items():
        try:
            html = fetch(url)
        except Exception as e:
            print(f"  [aviso] {fonte}: falhou na conexão ({e})", file=sys.stderr)
            continue

        texto = re.sub(r'<[^>]+>', ' ', html)
        texto = re.sub(r'\s+', ' ', texto)

        count = 0
        for m in RE_RODOVIA.finditer(texto):
            rod = m.group(1).replace(' ', '-')
            km = ""
            km_m = RE_KM.search(texto, m.end(), m.end() + 80)
            if km_m:
                km = "km " + km_m.group(1).strip()

            ctx = texto[max(0, m.start() - 80): m.end() + 120].strip()
            
            # Inferir UF a partir do código da rodovia (ex: ERS-630 -> RS)
            uf_inferida = ""
            prefixo = rod.split('-')[0]
            if prefixo in ["SP", "MG", "PR", "RJ", "RS", "SC", "BA", "GO"]:
                uf_inferida = prefixo
            elif prefixo in ["ERS", "VRS"]:
                uf_inferida = "RS"

            achados.append({
                "uf": uf_inferida,
                "rodovia": rod,
                "nome": "",
                "km": km,
                "local": "",
                "status": "total" if "total" in ctx.lower() else "parcial",
                "causa": ctx[:180],
                "fonte": fonte + " (auto)",
                "data": datetime.date.today().strftime("%d/%m/%Y"),
                "lat": None,
                "lon": None,
                "_auto": True
            })
            count += 1

        print(f"  [ok] {fonte}: {count} trechos identificados")
    return achados

def main():
    print("== Atualização de interdições rodoviárias ==")
    
    # 1. Carrega base manual
    manual = []
    if MANUAL.exists():
        try:
            manual = json.loads(MANUAL.read_text(encoding="utf-8"))
            print(f"Manual: {len(manual)} registros conferidos carregados")
        except Exception as e:
            print(f"[erro] Erro ao ler {MANUAL.name}: {e}", file=sys.stderr)
    else:
        print(f"[aviso] {MANUAL.name} não encontrado — criando arquivo base.")
        MANUAL.write_text("[]", encoding="utf-8")

    # Registros manuais validados possuem prioridade absoluta
    dados = [d for d in manual if d.get("lat") is not None and d.get("lon") is not None]
    vistos = {(d.get("uf", ""), d.get("rodovia", ""), d.get("km", "")) for d in dados}

    # 2. Executa o Scraper
    print("\nExecutando varredura automatizada...")
    auto = scrape_best_effort()

    # 3. Tenta geocodificar itens automáticos que não estejam na lista manual
    novos_adicionados = 0
    for a in auto:
        chave = (a.get("uf", ""), a.get("rodovia", ""), a.get("km", ""))
        if chave not in vistos:
            # Se tiver informações de cidade/local, tenta a geocodificação
            if a.get("local") and a.get("uf"):
                lat, lon = geocodificar_local(a["local"], a["uf"])
                a["lat"] = lat
                a["lon"] = lon

            if a.get("lat") is not None and a.get("lon") is not None:
                dados.append(a)
                vistos.add(chave)
                novos_adicionados += 1

    # 4. Grava a saída JSON consolidada
    payload = {
        "atualizado_em": datetime.datetime.now().strftime("%d/%m/%Y %H:%M"),
        "fontes": list(SOURCES.keys()),
        "total": len(dados),
        "interdicoes": dados,
    }
    
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    
    print(f"\n==========================================")
    print(f"Sucesso! {OUT.name} atualizado.")
    print(f"Total de pontos no mapa: {len(dados)}")
    print(f"Novos pontos capturados via auto-geocoding: {novos_adicionados}")
    print(f"==========================================")

if __name__ == "__main__":
    main()
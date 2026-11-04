
import json
import random
import re
import time
from datetime import datetime
from urllib.parse import quote

import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup, NavigableString
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

COLUNAS_SAIDA = ["Base de Origem", "Título", "Autores", "Ano", "Resumo", "Link", "String de Busca"]

# UA real evita bloqueio imediato por WAFs que detectam o UA padrao do Chromium headless
USER_AGENT_WINDOWS_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def extrair_ano(texto):
    """Procura um ano de 4 digitos (19xx/20xx) em texto livre."""
    if not texto:
        return None
    match = re.search(r"(19|20)\d{2}", texto)
    return match.group(0) if match else None


def sugerir_melhoria_busca(termo: str):
    """
    Heuristicas via regex para sugerir melhorias na string de busca (dica
    na UI, nunca bloqueia). Confirmado empiricamente que IEEE, RENOTE, SBIE
    e a Busca Integrada da RBIE interpretam AND/OR maiusculo corretamente.
    """
    if not termo or not termo.strip():
        return None

    termo = termo.strip()

    if termo.count('"') % 2 != 0:
        return (
            'Sua string tem aspas desbalanceadas (número ímpar de "). Verifique se cada '
            "frase exata está corretamente fechada entre aspas."
        )

    if termo.count("(") != termo.count(")"):
        return "Sua string tem parênteses desbalanceados — confira a quantidade de ( e )."

    tem_operador_maiusculo = bool(re.search(r"\b(AND|OR|NOT)\b", termo))
    tem_operador_minusculo = bool(re.search(r"\b(and|or|not)\b", termo)) and not tem_operador_maiusculo
    if tem_operador_minusculo:
        return (
            "Use AND/OR em maiúsculas — é o formato reconhecido como operador booleano pelas "
            "bases (ex.: 'termo1 AND termo2'), em minúsculas pode ser tratado apenas como mais "
            "uma palavra da busca."
        )

    palavras = re.findall(r"\w+", termo)
    if not tem_operador_maiusculo and len(palavras) > 1:
        return (
            "Sua busca tem várias palavras mas nenhum operador AND/OR. Para combinar conceitos "
            "diferentes use AND (ex.: 'terapia AND robô'); para sinônimos, use OR (ex.: "
            "'\"robô\" OR \"robótico\"')."
        )

    if len(palavras) == 1 and len(palavras[0]) < 5:
        return (
            "Sua busca é um termo único e curto — tende a trazer resultados pouco específicos. "
            "Considere combinar mais conceitos com AND/OR."
        )

    return None


def configurar_driver(headless: bool = True):
    """
    Configura o Selenium/Chrome em modo stealth: User-Agent real, remocao
    das flags de automacao, remocao de `navigator.webdriver` via CDP, e
    ignora erros de certificado SSL (redes institucionais com proxy).
    """
    options = Options()

    if headless:
        # "--headless=new" tem um bug conhecido no Windows: uma janela em
        # branco continua visivel durante toda a sessao, mesmo sem
        # "--disable-gpu". O modo classico e comprovadamente invisivel; sua
        # desvantagem teorica (UA contendo "HeadlessChrome") ja e neutralizada
        # pelo User-Agent sobrescrito abaixo.
        options.add_argument("--headless")
        options.add_argument("--window-position=-32000,-32000")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument(f"user-agent={USER_AGENT_WINDOWS_CHROME}")

    # Modo anonimo (incognito): garante que cache, cookies e historico de
    # buscas anteriores nao influenciem o tempo de coleta, e que o
    # comportamento seja o mesmo em qualquer maquina (nota: mesmo sem essa
    # flag, o Selenium ja cria um perfil novo e temporario a cada execucao
    # por padrao - o incognito reforça essa garantia de forma explicita)
    options.add_argument("--incognito")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-blink-features=AutomationControlled")

    options.add_experimental_option(
        "prefs", {"profile.default_content_setting_values.notifications": 2}
    )

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    # Sobrescreve navigator.webdriver antes do JS da pagina rodar (usado por
    # anti-bot como Cloudflare/PerimeterX/Akamai para detectar automacao)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"},
    )

    driver.set_page_load_timeout(45)
    return driver


def _pausa_anti_bloqueio():
    """Pausa entre requisicoes de paginacao (nao usada para esperar DOM)."""
    time.sleep(random.uniform(1.5, 3.0))


def _aceitar_cookies(driver, timeout: int = 5):
    """Fecha o banner de cookies (OneTrust) se aparecer; ausencia nao e erro."""
    # TODO: ATUALIZAR - adicionar seletor de outras ferramentas de consentimento se necessario
    seletores_possiveis = [
        "#onetrust-accept-btn-handler",
    ]
    for seletor in seletores_possiveis:
        try:
            botao = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, seletor))
            )
            botao.click()
            return
        except TimeoutException:
            continue


# ==============================================================================
# SCRAPER 1 - IEEE XPLORE
# ==============================================================================

def _buscar_ieee_via_api(driver, termo_busca: str, ano_inicio: int, ano_fim: int, numero_pagina: int, tamanho_pagina: int = 25):
    """
    Chama a API REST interna do IEEE Xplore (POST /rest/search) via fetch()
    dentro do contexto da pagina (execute_async_script), reaproveitando os
    cookies de sessao. Evita raspar o DOM da SPA (fragil) e resolve
    paginacao via "pageNumber" no payload, sem depender de clicar em botao
    (o botao de paginacao antigo parou de existir - bug encontrado nos testes).

    TODO: ATUALIZAR - endpoint nao documentado publicamente (engenharia
    reversa). Se parar de funcionar: Modo debug -> F12 -> aba Network ->
    filtro Fetch/XHR -> repetir busca manualmente -> inspecionar
    payload/resposta da requisicao "search" e ajustar o script abaixo e
    `_normalizar_registro_ieee`.

    Retorna o dict do JSON, ou None se falhar (endpoint mudou, erro de
    rede) - quem chama deve tratar o None caindo em fallback.
    """
    script = """
        const callback = arguments[arguments.length - 1];
        const [termo, tamanhoPagina, numeroPagina, intervaloAnos] = arguments;
        fetch("https://ieeexplore.ieee.org/rest/search", {
            method: "POST",
            credentials: "include",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                newsearch: true,
                queryText: termo,
                highlight: true,
                returnType: "SEARCH",
                matchPubs: true,
                rowsPerPage: tamanhoPagina,
                pageNumber: numeroPagina,
                ranges: [intervaloAnos]
            })
        })
        .then(r => {
            if (!r.ok) { throw new Error("HTTP " + r.status); }
            return r.json();
        })
        .then(dados => callback(JSON.stringify(dados)))
        .catch(erro => callback(JSON.stringify({"__erro__": String(erro)})));
    """
    try:
        driver.set_script_timeout(20)
        resposta_bruta = driver.execute_async_script(
            script, termo_busca, tamanho_pagina, numero_pagina, f"{ano_inicio}_{ano_fim}_Year"
        )
        dados = json.loads(resposta_bruta)
        if "__erro__" in dados:
            return None
        return dados
    except Exception:
        return None


def _normalizar_registro_ieee(registro: dict, string_busca_usada: str):
    """
    Converte um registro da API interna do IEEE para o formato padrao.

    TODO: ATUALIZAR - nomes de campo (ex.: "articleTitle", "abstract",
    "documentLink") observados na versao mais recente conhecida da API. Se
    a IEEE renomear campos, inspecione via DevTools (ver
    `_buscar_ieee_via_api`) e ajuste as chaves usadas abaixo.
    """
    titulo = registro.get("articleTitle") or registro.get("title") or "N/A"

    autores_lista = registro.get("authors") or []
    nomes_autores = [
        a.get("preferredName") or a.get("fullName") or a.get("normalizedName") or ""
        for a in autores_lista
    ]
    autores = "; ".join(nome for nome in nomes_autores if nome) or "N/A"

    ano = registro.get("publicationYear") or extrair_ano(str(registro.get("publicationTitle", ""))) or "N/A"

    resumo = registro.get("abstract") or "N/A"

    link_documento = registro.get("documentLink") or registro.get("htmlLink") or ""
    if link_documento and not str(link_documento).startswith("http"):
        link = f"https://ieeexplore.ieee.org{link_documento}"
    else:
        link = link_documento or "N/A"

    return {
        "Base de Origem": "IEEE Xplore",
        "Título": titulo,
        "Autores": autores,
        "Ano": str(ano),
        "Resumo": resumo,
        "Link": link,
        "String de Busca": string_busca_usada,
    }


def _scraper_ieee_dom_fallback(driver, string_busca_usada: str):
    """
    Fallback usado SOMENTE se a API interna falhar. Raspagem tradicional de
    HTML, sem paginacao (so 1a pagina).

    TODO: ATUALIZAR - seletores CSS sujeitos a desatualizar com mudancas visuais do site.
    """
    SELETOR_CARD = "div.List-results-items"
    SELETOR_TITULO = "h3"
    SELETOR_AUTORES = "p.author"
    SELETOR_RESUMO = "div.description-text"

    resultados = []
    try:
        WebDriverWait(driver, 20).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, SELETOR_CARD)))
        soup = BeautifulSoup(driver.page_source, "html.parser")
        cards = soup.select(SELETOR_CARD)

        for card in cards:
            try:
                titulo_tag = card.select_one(SELETOR_TITULO)
                titulo = titulo_tag.get_text(strip=True) if titulo_tag else "N/A"

                link_tag = card.select_one("a")
                href = link_tag.get("href") if link_tag else None
                link = f"https://ieeexplore.ieee.org{href}" if href else "N/A"

                autores_tag = card.select_one(SELETOR_AUTORES)
                autores = autores_tag.get_text(strip=True) if autores_tag else "N/A"

                resumo_tag = card.select_one(SELETOR_RESUMO)
                resumo = resumo_tag.get_text(strip=True) if resumo_tag else "N/A"

                ano = extrair_ano(card.get_text(" ", strip=True)) or "N/A"

                resultados.append(
                    {
                        "Base de Origem": "IEEE Xplore",
                        "Título": titulo,
                        "Autores": autores,
                        "Ano": ano,
                        "Resumo": resumo,
                        "Link": link,
                        "String de Busca": string_busca_usada,
                    }
                )
            except Exception:
                continue
    except TimeoutException:
        pass

    return resultados


def scraper_ieee(driver, termo_busca: str, ano_inicio: int, ano_fim: int, max_paginas: int = 1, buscar_resumo: bool = False):
    """
    Extrai artigos do IEEE Xplore via API interna (`_buscar_ieee_via_api`),
    com fallback para raspagem de HTML se a API falhar.

    Resiliencia: qualquer erro e capturado e reportado via st.error/warning;
    a funcao sempre retorna lista (mesmo vazia), nunca propaga excecao.

    `buscar_resumo` e ignorado aqui (assinatura uniforme entre scrapers) -
    a API do IEEE ja retorna o resumo completo sem custo extra.
    """
    resultados = []

    try:
        query_codificada = quote(termo_busca)
        url = (
            "https://ieeexplore.ieee.org/search/searchresult.jsp"
            f"?newsearch=true&queryText={query_codificada}"
            f"&ranges={ano_inicio}_{ano_fim}_Year"
        )
        driver.get(url)
        _aceitar_cookies(driver)

        for pagina in range(1, max_paginas + 1):
            dados_pagina = _buscar_ieee_via_api(driver, termo_busca, ano_inicio, ano_fim, pagina)

            if dados_pagina is None:
                if pagina == 1:
                    st.warning(
                        "IEEE Xplore: API interna indisponível, tentando raspagem tradicional do HTML "
                        "(sem suporte a múltiplas páginas nesse modo)."
                    )
                    resultados.extend(_scraper_ieee_dom_fallback(driver, termo_busca))
                break

            registros = dados_pagina.get("records", [])
            if not registros:
                if pagina == 1:
                    st.warning(
                        "IEEE Xplore: a busca não retornou nenhum artigo (0 resultados para o termo/período informado)."
                    )
                break

            for registro in registros:
                try:
                    resultados.append(_normalizar_registro_ieee(registro, termo_busca))
                except Exception:
                    continue

            if pagina < max_paginas:
                _pausa_anti_bloqueio()

    except TimeoutException:
        st.error(
            "IEEE Xplore: tempo de espera esgotado ao carregar a página de busca. "
            "Pode ser bloqueio anti-bot."
        )
    except WebDriverException as e:
        st.error(f"IEEE Xplore: erro no navegador/driver - {e}")
    except Exception as e:
        st.error(f"IEEE Xplore: erro inesperado - {e}")

    return resultados


# ==============================================================================
# SCRAPER 2 - ACM DIGITAL LIBRARY
# ==============================================================================

def scraper_acm(driver, termo_busca: str, ano_inicio: int, ano_fim: int, max_paginas: int = 1, buscar_resumo: bool = False):
    """
    Extrai artigos da ACM Digital Library (plataforma Atypon).

    `buscar_resumo` e ignorado aqui: o card de resultado ja inclui um
    trecho do resumo, sem custo de requisicao extra.
    """
    resultados = []

    # TODO: ATUALIZAR - seletores da pagina de busca (dl.acm.org/action/doSearch)
    SELETOR_CARD = "div.issue-item, li.search__item"
    SELETOR_TITULO = "h5.issue-item__title a, h3.issue-item__title a"
    SELETOR_AUTORES = "ul.rlist--inline.loa a"
    SELETOR_RESUMO = "div.issue-item__abstract"
    SELETOR_DATA = "span.CitationCoverDate, div.bookPubDate"
    SELETOR_BOTAO_PROXIMA_PAGINA = "a.pagination__btn--next"

    try:
        query_codificada = quote(termo_busca)
        url = (
            "https://dl.acm.org/action/doSearch"
            f"?AllField={query_codificada}&AfterYear={ano_inicio}&BeforeYear={ano_fim}"
        )
        driver.get(url)
        _aceitar_cookies(driver)

        for pagina in range(1, max_paginas + 1):
            wait = WebDriverWait(driver, 20)
            wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, SELETOR_CARD)))

            soup = BeautifulSoup(driver.page_source, "html.parser")
            cards = soup.select(SELETOR_CARD)

            if not cards:
                st.warning(
                    f"ACM Digital Library: nenhum card encontrado na página {pagina}. "
                    "Os seletores provavelmente estão desatualizados (ver TODOs em scraper_acm)."
                )
                break

            for card in cards:
                try:
                    titulo_tag = card.select_one(SELETOR_TITULO)
                    titulo = titulo_tag.get_text(strip=True) if titulo_tag else "N/A"
                    href = titulo_tag.get("href") if titulo_tag else None
                    link = f"https://dl.acm.org{href}" if href else "N/A"

                    autores_tags = card.select(SELETOR_AUTORES)
                    autores = (
                        ", ".join(a.get_text(strip=True) for a in autores_tags)
                        if autores_tags
                        else "N/A"
                    )

                    resumo_tag = card.select_one(SELETOR_RESUMO)
                    resumo = resumo_tag.get_text(strip=True) if resumo_tag else "N/A"

                    data_tag = card.select_one(SELETOR_DATA)
                    texto_data = data_tag.get_text(strip=True) if data_tag else card.get_text(" ", strip=True)
                    ano = extrair_ano(texto_data) or "N/A"

                    resultados.append(
                        {
                            "Base de Origem": "ACM Digital Library",
                            "Título": titulo,
                            "Autores": autores,
                            "Ano": ano,
                            "Resumo": resumo,
                            "Link": link,
                            "String de Busca": termo_busca,
                        }
                    )
                except Exception:
                    continue

            if pagina < max_paginas:
                try:
                    botao_proxima = driver.find_element(By.CSS_SELECTOR, SELETOR_BOTAO_PROXIMA_PAGINA)
                    driver.execute_script("arguments[0].click();", botao_proxima)
                    _pausa_anti_bloqueio()
                except NoSuchElementException:
                    break

    except TimeoutException:
        st.error(
            "ACM Digital Library: tempo de espera esgotado ao carregar os resultados. "
            "Pode ser bloqueio anti-bot ou seletores desatualizados."
        )
    except WebDriverException as e:
        st.error(f"ACM Digital Library: erro no navegador/driver - {e}")
    except Exception as e:
        st.error(f"ACM Digital Library: erro inesperado - {e}")

    return resultados


# ==============================================================================
# SCRAPER 3 - SCOPUS
# ==============================================================================

def scraper_scopus(driver, termo_busca: str, ano_inicio: int, ano_fim: int, max_paginas: int = 1, buscar_resumo: bool = False):
    """
    Extrai artigos da Scopus (Elsevier).

    ATENCAO: a Scopus normalmente EXIGE autenticacao institucional (SSO)
    para exibir resultados. Se retornar 0/timeout, o motivo provavel e
    falta de sessao autenticada, nao seletor desatualizado. Alternativas:
    (1) Modo Debug + login manual antes de buscar; (2) Scopus Search API
    oficial (dev.elsevier.com) para uso em producao.

    `buscar_resumo` e ignorado aqui: o card ja inclui o resumo.
    """
    resultados = []

    # TODO: ATUALIZAR - seletores da pagina de resultados (scopus.com/results/results.uri)
    SELETOR_CARD = "li.searchArea-results-item, tr.resultsRow"
    SELETOR_TITULO = "a.ddmDocTitle, a.resultsTitle"
    SELETOR_AUTORES = "span.author, div.authorList"
    SELETOR_RESUMO = "div.resultsAbstract"
    SELETOR_BOTAO_PROXIMA_PAGINA = "a[aria-label='Next page']"

    try:
        # TITLE-ABS-KEY(...) e a sintaxe oficial da Scopus (busca em titulo,
        # resumo e palavras-chave) - e a string REALMENTE enviada, por isso
        # e ela (nao o termo_busca puro) que fica na coluna "String de Busca"
        string_busca_usada = f"TITLE-ABS-KEY({termo_busca})"
        query_codificada = quote(string_busca_usada)
        url = (
            "https://www.scopus.com/results/results.uri"
            f"?query={query_codificada}&yearFrom={ano_inicio}&yearTo={ano_fim}"
        )
        driver.get(url)
        _aceitar_cookies(driver)

        for pagina in range(1, max_paginas + 1):
            wait = WebDriverWait(driver, 25)
            wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, SELETOR_CARD)))

            soup = BeautifulSoup(driver.page_source, "html.parser")
            cards = soup.select(SELETOR_CARD)

            if not cards:
                st.warning(
                    f"Scopus: nenhum card encontrado na página {pagina}. "
                    "Verifique se a sessão está autenticada (SSO institucional) "
                    "ou se os seletores estão desatualizados (ver TODOs em scraper_scopus)."
                )
                break

            for card in cards:
                try:
                    titulo_tag = card.select_one(SELETOR_TITULO)
                    titulo = titulo_tag.get_text(strip=True) if titulo_tag else "N/A"
                    href = titulo_tag.get("href") if titulo_tag else None
                    link = href if href else "N/A"

                    autores_tags = card.select(SELETOR_AUTORES)
                    autores = (
                        ", ".join(a.get_text(strip=True) for a in autores_tags)
                        if autores_tags
                        else "N/A"
                    )

                    resumo_tag = card.select_one(SELETOR_RESUMO)
                    resumo = resumo_tag.get_text(strip=True) if resumo_tag else "N/A"

                    ano = extrair_ano(card.get_text(" ", strip=True)) or "N/A"

                    resultados.append(
                        {
                            "Base de Origem": "Scopus",
                            "Título": titulo,
                            "Autores": autores,
                            "Ano": ano,
                            "Resumo": resumo,
                            "Link": link,
                            "String de Busca": string_busca_usada,
                        }
                    )
                except Exception:
                    continue

            if pagina < max_paginas:
                try:
                    botao_proxima = driver.find_element(By.CSS_SELECTOR, SELETOR_BOTAO_PROXIMA_PAGINA)
                    driver.execute_script("arguments[0].click();", botao_proxima)
                    _pausa_anti_bloqueio()
                except NoSuchElementException:
                    break

    except TimeoutException:
        st.error(
            "Scopus: tempo de espera esgotado. Provavelmente exige login institucional "
            "(SSO) - verifique se a sessão do navegador está autenticada."
        )
    except WebDriverException as e:
        st.error(f"Scopus: erro no navegador/driver - {e}")
    except Exception as e:
        st.error(f"Scopus: erro inesperado - {e}")

    return resultados


# ==============================================================================
# SCRAPERS 4, 5 E 6 - BASES BRASILEIRAS (RBIE, RENOTE, SBIE) - PLATAFORMA OJS
# ==============================================================================
# RBIE e SBIE rodam na plataforma SOL (SBC Open Library); RENOTE na SEER da
# UFRGS - todas baseadas em OJS. A logica de raspagem comum fica em
# `_extrair_artigos_ojs`/`_scraper_ojs_generico`, reaproveitada por
# `scraper_rbie`, `scraper_renote` e `scraper_sbie`, que continuam sendo
# funcoes proprias (uma por base) chamadas individualmente pelo orquestrador.


def _texto_ate_proximo_link(link, padrao_href: str = "/article/view/"):
    """
    Caminha pelo DOM a partir de `link` ate o PROXIMO link de artigo,
    coletando so o texto entre eles. Usado quando a pagina de resultados
    nao separa cada resultado em um container HTML proprio (ex.: Busca
    Integrada da SOL, usada pelo scraper_rbie) - nesse caso, find_parent
    pegaria o texto de varios resultados misturados.
    """
    partes = []
    for elemento in link.next_elements:
        # next_elements inclui primeiro os proprios descendentes do link
        # (o texto do titulo) antes do que vem depois - precisam ser pulados
        if link in getattr(elemento, "parents", []):
            continue

        href = getattr(elemento, "get", lambda *_: None)("href")
        if getattr(elemento, "name", None) == "a" and href and padrao_href in href:
            break
        if isinstance(elemento, NavigableString):
            texto = str(elemento).strip()
            if texto:
                partes.append(texto)
    return " ".join(partes)


def _extrair_artigos_ojs(driver, nome_base: str, string_busca_usada: str):
    """
    Extrai artigos de uma pagina de resultados OJS usando um padrao comum
    a qualquer instalacao: o link do artigo sempre aponta para uma URL com
    "/article/view/<id>" (evita depender de classes CSS do tema visual,
    que variam entre instalacoes).

    Autores/ano sao extraidos por heuristica de texto (sem classe CSS
    garantida): pega o texto ao redor do link, usa `extrair_ano()` para o
    ano e o texto antes do primeiro numero para os autores.

    TODO: ATUALIZAR - se a extracao de autores vier ruim para alguma base,
    inspecione o HTML real (F12) e troque a heuristica por um seletor
    especifico do tema, como feito para IEEE/ACM/Scopus.

    LIMITACAO: o resumo nao aparece na listagem OJS, so na pagina de
    detalhe do artigo - fica "N/A" aqui (ver `_enriquecer_via_detalhe_ojs`).
    """
    resultados = []
    soup = BeautifulSoup(driver.page_source, "html.parser")

    links_vistos = set()
    for link in soup.select("a[href*='/article/view/']"):
        href = link.get("href")
        titulo = link.get_text(strip=True)
        if not href or not titulo or href in links_vistos:
            continue
        links_vistos.add(href)

        # "obj_article_summary" e a classe do tema padrao OJS 3.x para o
        # card inteiro (titulo+autores+data); o parent imediato (div.title)
        # traria so o titulo, por isso subimos ate esse container especifico
        bloco = link.find_parent("div", class_="obj_article_summary")
        if bloco is None:
            bloco = link.find_parent(["div", "li", "tr"]) or link.parent
        texto_bloco = bloco.get_text(" ", strip=True) if bloco else titulo

        ano = extrair_ano(texto_bloco) or "N/A"

        resto = texto_bloco.replace(titulo, "", 1).strip()
        match_autores = re.match(r"^([^\d]+)", resto)
        autores = match_autores.group(1).strip(" ,;-") if match_autores else ""
        autores = autores or "N/A"

        resultados.append(
            {
                "Base de Origem": nome_base,
                "Título": titulo,
                "Autores": autores,
                "Ano": ano,
                "Resumo": "N/A",
                "Link": href,
                "String de Busca": string_busca_usada,
            }
        )

    return resultados


def _enriquecer_via_detalhe_ojs(driver, artigo: dict, buscar_resumo: bool, timeout: int = 15):
    """
    Visita a pagina de DETALHE do artigo, so quando necessario, para:
      1. Corrigir Autores/Ano vindos "N/A" da heuristica da listagem, lendo
         as tags <meta name="citation_author"> e <meta name="citation_date">
         (padrao usado por praticamente toda instalacao OJS, o mesmo lido
         pelo Google Scholar - mais confiavel que a heuristica de texto).
      2. Extrair o resumo completo de "div.item.abstract", se `buscar_resumo`.

    Um artigo ja completo da listagem e buscar_resumo=False nao geram
    nenhuma requisicao extra. Falha (timeout, pagina fora do ar) mantem os
    valores que a listagem ja tinha.

    TODO: ATUALIZAR - "div.item.abstract" e as metatags citation_* sao o
    padrao do tema/plugin de citacao do OJS 3.x (confirmado no SBIE).
    """
    precisa_autor = artigo["Autores"] == "N/A"
    precisa_ano = artigo["Ano"] == "N/A"
    if not (precisa_autor or precisa_ano or buscar_resumo):
        return artigo

    try:
        driver.get(artigo["Link"])
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "meta[name='citation_title'], div.item.abstract, section.item.abstract")
            )
        )
        soup = BeautifulSoup(driver.page_source, "html.parser")

        if precisa_autor:
            nomes = [m.get("content", "").strip() for m in soup.select("meta[name='citation_author']")]
            nomes = [n for n in nomes if n]
            if nomes:
                artigo["Autores"] = "; ".join(nomes)

        if precisa_ano:
            meta_data = soup.select_one("meta[name='citation_date']")
            if meta_data and meta_data.get("content"):
                ano_extraido = extrair_ano(meta_data["content"])
                if ano_extraido:
                    artigo["Ano"] = ano_extraido

        if buscar_resumo:
            bloco = soup.select_one("div.item.abstract, section.item.abstract")
            if bloco:
                texto = bloco.get_text(" ", strip=True)
                texto = re.sub(r"^(Resumo|Abstract)\s*", "", texto, flags=re.IGNORECASE).strip()
                artigo["Resumo"] = texto or "N/A"
    except Exception:
        pass

    return artigo


def _scraper_ojs_generico(driver, nome_base: str, url_base: str, termo_busca: str, ano_inicio: int, ano_fim: int, max_paginas: int = 1, buscar_resumo: bool = False):
    """
    Logica de raspagem compartilhada pelas bases OJS (RBIE, RENOTE, SBIE).
    Paginacao via parametro de URL "searchPage" (confirmado nos testes),
    sem clique em botao.

    Filtro de ano via "dateFromYear"/"dateToYear" (busca avancada do OJS
    3.x). TODO: ATUALIZAR se alguma base ignorar esses parametros - ha um
    filtro de seguranca local abaixo (descarta artigos fora do intervalo)
    que garante o comportamento correto mesmo assim.
    """
    resultados = []

    try:
        query_codificada = quote(termo_busca)

        for pagina in range(1, max_paginas + 1):
            url = (
                f"{url_base}/search/search"
                f"?query={query_codificada}"
                f"&dateFromYear={ano_inicio}&dateToYear={ano_fim}"
                f"&searchPage={pagina}"
            )
            driver.get(url)
            _aceitar_cookies(driver, timeout=3)

            WebDriverWait(driver, 20).until(
                lambda d: "Nenhum resultado" in d.page_source
                or "no result" in d.page_source.lower()
                or d.find_elements(By.CSS_SELECTOR, "a[href*='/article/view/']")
            )

            artigos_pagina = _extrair_artigos_ojs(driver, nome_base, termo_busca)

            if not artigos_pagina:
                if pagina == 1:
                    st.warning(f"{nome_base}: nenhum artigo encontrado para o termo/período informado.")
                break

            for artigo in artigos_pagina:
                precisa_visita = buscar_resumo or artigo["Autores"] == "N/A" or artigo["Ano"] == "N/A"
                if precisa_visita:
                    artigo = _enriquecer_via_detalhe_ojs(driver, artigo, buscar_resumo)
                    _pausa_anti_bloqueio()

                # Filtro aplicado APOS o enriquecimento, usando o ano mais confiavel disponivel
                ano_txt = artigo["Ano"]
                if ano_txt.isdigit() and not (ano_inicio <= int(ano_txt) <= ano_fim):
                    continue

                resultados.append(artigo)

            if pagina < max_paginas:
                _pausa_anti_bloqueio()

    except TimeoutException:
        st.error(f"{nome_base}: tempo de espera esgotado ao carregar os resultados.")
    except WebDriverException as e:
        st.error(f"{nome_base}: erro no navegador/driver - {e}")
    except Exception as e:
        st.error(f"{nome_base}: erro inesperado - {e}")

    return resultados


def scraper_rbie(driver, termo_busca: str, ano_inicio: int, ano_fim: int, max_paginas: int = 1, buscar_resumo: bool = False):
    """
    Extrai artigos da RBIE (Revista Brasileira de Informática na Educação).

    ACHADO: a busca nativa do OJS da RBIE (.../rbie/search/search) retorna
    sempre "Nenhum resultado encontrado", mesmo pelo formulario oficial do
    site com termos genericos - limitacao do lado da RBIE (indice de busca
    aparentemente vazio/quebrado), nao um bug do scraper.

    Por isso esta funcao usa a "Busca Integrada" da SOL (SBC Open Library),
    que de fato indexa o conteudo da RBIE:
    https://sol.sbc.org.br/busca/index.php/integrada/results
    Essa busca e cruzada entre todas as bases da SOL (selecionadas via
    "archiveIds[]", onde 2 = Periodicos) - os resultados sao filtrados
    manualmente para manter so links "/rbie/".

    TODO: ATUALIZAR - paginacao NAO confirmada para este endpoint (testes
    sempre couberam em 1 pagina). Investigar via Modo Debug + F12 + Network
    se uma busca futura gerar mais resultados do que cabem em uma pagina.
    """
    resultados = []
    ARCHIVE_ID_PERIODICOS = 2  # "archiveIds[]=2" = categoria "Periódicos" no formulário da SOL
    NOME_JORNAL_RBIE = "REVISTA BRASILEIRA DE INFORMÁTICA NA EDUCAÇÃO"

    try:
        query_codificada = quote(termo_busca)
        url = (
            "https://sol.sbc.org.br/busca/index.php/integrada/results"
            f"?isAdvanced=1&archiveIds%5B%5D={ARCHIVE_ID_PERIODICOS}&query={query_codificada}"
        )
        driver.get(url)
        _aceitar_cookies(driver, timeout=3)

        WebDriverWait(driver, 20).until(
            lambda d: "Nenhum resultado" in d.page_source
            or d.find_elements(By.CSS_SELECTOR, "a[href*='/rbie/article/view/']")
        )

        soup = BeautifulSoup(driver.page_source, "html.parser")
        links_vistos = set()

        for link in soup.select("a[href*='/rbie/article/view/']"):
            href = link.get("href")
            titulo = link.get_text(strip=True)
            if not href or not titulo or href in links_vistos:
                continue
            links_vistos.add(href)

            # Esta pagina nao separa os resultados em containers HTML
            # individuais, entao caminhamos do link do titulo ate o
            # proximo link de artigo, coletando so o texto deste resultado
            texto_apos_titulo = _texto_ate_proximo_link(link)

            ano = extrair_ano(texto_apos_titulo) or "N/A"

            # Formato tipico: "<autores separados por ';'> <NOME DO
            # PERIODICO> <data ISO> ...". Corta no nome do periodico
            # (conhecido, ja que filtramos so links da RBIE) para isolar autores.
            autores = re.split(NOME_JORNAL_RBIE, texto_apos_titulo, flags=re.IGNORECASE)[0]
            autores = autores.strip(" ,;-") or "N/A"

            item = {
                "Base de Origem": "RBIE",
                "Título": titulo,
                "Autores": autores,
                "Ano": ano,
                "Resumo": "N/A",
                "Link": href,
                "String de Busca": termo_busca,
            }

            precisa_visita = buscar_resumo or item["Autores"] == "N/A" or item["Ano"] == "N/A"
            if precisa_visita:
                item = _enriquecer_via_detalhe_ojs(driver, item, buscar_resumo)
                _pausa_anti_bloqueio()

            ano_txt = item["Ano"]
            if ano_txt.isdigit() and not (ano_inicio <= int(ano_txt) <= ano_fim):
                continue

            resultados.append(item)

        if not resultados:
            st.warning("RBIE: nenhum artigo encontrado para o termo/período informado.")

    except TimeoutException:
        st.error("RBIE: tempo de espera esgotado ao carregar os resultados.")
    except WebDriverException as e:
        st.error(f"RBIE: erro no navegador/driver - {e}")
    except Exception as e:
        st.error(f"RBIE: erro inesperado - {e}")

    return resultados


def scraper_renote(driver, termo_busca: str, ano_inicio: int, ano_fim: int, max_paginas: int = 1, buscar_resumo: bool = False):
    """Extrai artigos da RENOTE (plataforma SEER da UFRGS, também OJS)."""
    return _scraper_ojs_generico(
        driver, "RENOTE", "https://seer.ufrgs.br/renote",
        termo_busca, ano_inicio, ano_fim, max_paginas, buscar_resumo,
    )


def scraper_sbie(driver, termo_busca: str, ano_inicio: int, ano_fim: int, max_paginas: int = 1, buscar_resumo: bool = False):
    """Extrai artigos dos Anais do SBIE (plataforma SOL, busca nativa - ver decisão de escopo em scraper_rbie)."""
    return _scraper_ojs_generico(
        driver, "SBIE", "https://sol.sbc.org.br/index.php/sbie",
        termo_busca, ano_inicio, ano_fim, max_paginas, buscar_resumo,
    )


# ==============================================================================
# ORQUESTRADOR PRINCIPAL (INTERFACE STREAMLIT)
# ==============================================================================

def main():
    st.set_page_config(page_title="RSL Scraper - TCC", layout="wide")

    st.title("Coleta Automatizada para Revisão Sistemática de Literatura")
    st.caption(
        "TCC em Sistemas de Informação — coleta multi-base: IEEE Xplore, ACM Digital Library, "
        "Scopus, RBIE, RENOTE e SBIE"
    )

    with st.sidebar:
        st.header("Parâmetros de busca")
        termo_busca = st.text_input("String de busca", value="Ensino AND IA")

        sugestao_busca = sugerir_melhoria_busca(termo_busca)
        if sugestao_busca:
            st.caption(f"Sugestão: {sugestao_busca}")

        col1, col2 = st.columns(2)
        ano_inicio = col1.number_input("Ano inicial", min_value=1990, max_value=2100, value=2023, step=1)
        ano_fim = col2.number_input("Ano final", min_value=1990, max_value=2100, value=2025, step=1)

        opcoes_bases = [
            "IEEE Xplore",
            "ACM Digital Library",
            "Scopus",
            "RBIE",
            "RENOTE",
            "SBIE",
        ]
        bases_selecionadas = st.multiselect(
            "Bases de dados",
            opcoes_bases,
            default=["RBIE", "RENOTE", "SBIE"],
        )

        max_paginas = st.slider(
            "Máximo de páginas por base", min_value=1, max_value=10, value=1,
            help="Cada página adicional gera mais requisições - aumente com moderação para evitar bloqueios.",
        )

        buscar_resumo = st.checkbox(
            "Buscar resumo completo (mais lento)",
            value=False,
            help=(
                "Para RBIE, RENOTE e SBIE, o resumo não aparece na listagem de busca — só na "
                "página de cada artigo. Ativar esta opção abre a página de cada artigo "
                "individualmente para trazer o resumo completo, o que deixa a coleta bem mais "
                "lenta. Não afeta IEEE Xplore, ACM Digital Library e Scopus, que já trazem o "
                "resumo na própria listagem."
            ),
        )

        modo_debug = st.checkbox(
            "Modo debug (exibir navegador)",
            value=False,
            help="Desliga o modo headless para você acompanhar o Chrome e ajustar seletores quebrados.",
        )

        iniciar = st.button("Iniciar coleta", type="primary", use_container_width=True)

    if not iniciar:
        st.info("Configure os parâmetros na barra lateral e clique em Iniciar coleta.")
        return

    if ano_inicio > ano_fim:
        st.error("O ano inicial não pode ser maior que o ano final.")
        return
    if not termo_busca.strip():
        st.error("Informe uma string de busca.")
        return
    if not bases_selecionadas:
        st.error("Selecione ao menos uma base de dados.")
        return

    driver = None
    try:
        with st.spinner("Inicializando o navegador (modo stealth)..."):
            driver = configurar_driver(headless=not modo_debug)
    except Exception as e:
        st.error(f"Não foi possível inicializar o WebDriver: {e}")
        return

    scrapers_disponiveis = {
        "IEEE Xplore": scraper_ieee,
        "ACM Digital Library": scraper_acm,
        "Scopus": scraper_scopus,
        "RBIE": scraper_rbie,
        "RENOTE": scraper_renote,
        "SBIE": scraper_sbie,
    }

    todos_resultados = []
    tempos_por_base = {}
    progresso = st.progress(0.0)
    total_bases = len(bases_selecionadas)
    tempo_inicio_total = time.time()
    tempo_total = 0.0

    try:
        for i, base in enumerate(bases_selecionadas):
            placeholder_status = st.empty()
            placeholder_status.info(f"Coletando dados de {base}...")

            tempo_inicio_base = time.time()
            try:
                funcao_scraper = scrapers_disponiveis[base]
                resultado_base = funcao_scraper(driver, termo_busca, ano_inicio, ano_fim, max_paginas, buscar_resumo)
                todos_resultados.extend(resultado_base)
                tempo_decorrido = time.time() - tempo_inicio_base
                tempos_por_base[base] = tempo_decorrido
                placeholder_status.success(
                    f"{base}: {len(resultado_base)} artigo(s) coletado(s) em {tempo_decorrido:.1f}s."
                )
            except Exception as e:
                tempo_decorrido = time.time() - tempo_inicio_base
                tempos_por_base[base] = tempo_decorrido
                # Falha nao prevista aqui tambem nao interrompe as demais bases
                placeholder_status.error(
                    f"{base}: falhou inesperadamente após {tempo_decorrido:.1f}s ({e}). "
                    "Prosseguindo para a próxima base."
                )

            progresso.progress((i + 1) / total_bases)

        # Medido ANTES do driver.quit() no finally, para bater com a soma
        # dos tempos individuais (fechar o navegador nao conta como tempo
        # de busca)
        tempo_total = time.time() - tempo_inicio_total
    finally:
        driver.quit()

    st.divider()

    if not todos_resultados:
        st.warning(
            "Nenhum resultado foi coletado em nenhuma base. Ative o Modo debug e "
            "verifique se os seletores XPath/CSS precisam de atualização."
        )
        return

    df = pd.DataFrame(todos_resultados, columns=COLUNAS_SAIDA)

    # Ordem inicial: mais recente primeiro. A partir daqui, o usuario pode
    # clicar no cabecalho da coluna "Ano" na tabela abaixo para reordenar
    # (recurso nativo do st.dataframe) sem precisar refazer a busca.
    df["_ano_ordenacao"] = pd.to_numeric(df["Ano"], errors="coerce")
    df = df.sort_values("_ano_ordenacao", ascending=False, na_position="last").drop(columns="_ano_ordenacao")
    df = df.reset_index(drop=True)

    st.subheader("Resumo da coleta")
    st.caption(f"Tempo total da coleta: {tempo_total:.1f}s")

    # Cores dos badges de tempo: comparam apenas VELOCIDADE relativa entre
    # as bases rodadas nesta coleta, nao "qualidade" - uma base mais lenta
    # muitas vezes so esta fazendo mais trabalho legitimo (ex.: visitando a
    # pagina de cada artigo para completar autor/ano faltante).
    tempos_validos = [t for t in tempos_por_base.values() if t is not None]
    tempo_min = min(tempos_validos) if tempos_validos else None
    tempo_max = max(tempos_validos) if tempos_validos else None

    def _cor_tempo(tempo):
        if tempo is None or tempo_min is None or tempo_min == tempo_max:
            return "#9AA0A6"  # cinza neutro (sem base de comparacao)
        if tempo == tempo_min:
            return "#1E8E3E"  # mais rapida
        if tempo == tempo_max:
            return "#D93025"  # mais lenta
        return "#F9AB00"  # intermediaria

    colunas_metricas = st.columns(len(bases_selecionadas) + 1)
    colunas_metricas[0].metric("Total geral", len(df))
    for idx, base in enumerate(bases_selecionadas, start=1):
        contagem = int((df["Base de Origem"] == base).sum())
        tempo_base = tempos_por_base.get(base)
        with colunas_metricas[idx]:
            st.metric(base, contagem)
            if tempo_base is not None:
                cor = _cor_tempo(tempo_base)
                st.markdown(
                    f'<span style="background-color:{cor}22; color:{cor}; padding:2px 10px; '
                    f'border-radius:12px; font-size:0.85rem; font-weight:600;">{tempo_base:.1f}s</span>',
                    unsafe_allow_html=True,
                )

    if len(tempos_validos) > 1 and tempo_min != tempo_max:
        st.caption(
            "Cor do tempo = velocidade relativa entre as bases desta coleta (verde = mais "
            "rápida, vermelho = mais lenta), não uma avaliação de qualidade — uma base mais "
            "lenta costuma estar apenas completando mais dados (ex.: autor/ano ausentes)."
        )

    st.subheader("Resultados consolidados")
    st.caption(
        "Ordenados por ano de publicação (mais recente primeiro). Clique no cabeçalho da "
        "coluna \"Ano\" na tabela para reordenar (crescente/decrescente) sem refazer a busca."
    )
    st.dataframe(df, use_container_width=True)

    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    nome_arquivo = f"rsl_resultados_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    # Botao de download do CSV: borda/texto verde (cor do Excel) sempre
    # visiveis, preenchendo de verde solido ao passar o mouse
    st.markdown(
        """
        <style>
        div[data-testid="stDownloadButton"] button {
            border: 2px solid #217346 !important;
            color: #217346 !important;
            font-weight: 600 !important;
        }
        div[data-testid="stDownloadButton"] button:hover {
            background-color: #217346 !important;
            color: #FFFFFF !important;
            border-color: #217346 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.download_button("Baixar CSV consolidado", csv_bytes, nome_arquivo, "text/csv", use_container_width=True)


if __name__ == "__main__":
    main()

# Contexto do Software Desenvolvido — TCC de Luiz Henrique Martini de Souza

Este documento contextualiza uma ferramenta de software já desenvolvida e testada, para que você (IA) escreva o texto do Trabalho de Conclusão de Curso (TCC) no Overleaf (LaTeX), com precisão técnica sobre o que foi de fato implementado. Trate as informações abaixo como a fonte de verdade sobre o software — não invente funcionalidades, arquiteturas ou resultados que não estejam descritos aqui. Se precisar de algum dado que não está aqui (ex.: números exatos de uma tabela, gráfico), pergunte antes de inventar.

**Formato de entrega esperado**: texto em LaTeX, pronto para colar no Overleaf, seguindo a formatação ABNT usual de TCCs de Sistemas de Informação no Brasil (a menos que o aluno informe um template/classe específica do Overleaf — pergunte se não souber qual `\documentclass` ou template a instituição exige).

## 1. Contexto acadêmico

- **Curso**: Sistemas de Informação.
- **Tema do TCC**: automação da coleta de artigos científicos para Revisão Sistemática de Literatura (RSL), usando Web Scraping.
- **Problema de pesquisa**: a coleta manual de artigos em múltiplas bases acadêmicas é um gargalo demorado e sujeito a erro em processos de RSL. O trabalho propõe uma ferramenta que automatiza a etapa de **busca e coleta** (fase "Identification" de frameworks como o PRISMA), consolidando os resultados de várias bases em um único CSV padronizado — deixando a etapa de **triagem por relevância** (leitura de título/resumo) como trabalho humano subsequente, deliberadamente fora do escopo da ferramenta.
- **Cenário de aplicação**: Múltiplas Fontes — a ferramenta recebe uma string de busca e um intervalo de anos, e raspa simultaneamente 6 bases acadêmicas diferentes numa única execução.

## 2. Stack tecnológica

- **Interface**: Streamlit (Python), tema visual customizado (claro, cor primária azul-ardósia `#2C3E50`, sem emojis — decisão de design tomada após feedback de que o visual padrão do Streamlit "parecia amador").
- **Extração de páginas dinâmicas/com bloqueio**: Selenium + `webdriver-manager` (Chrome).
- **Parsing de HTML**: BeautifulSoup4.
- **Manipulação de dados**: Pandas (consolidação em DataFrame e exportação CSV UTF-8 com BOM).
- Todo o software está em um único arquivo `app.py`, com um `requirements.txt` e um `.streamlit/config.toml` (tema visual).

## 3. Inputs e outputs da ferramenta

**Inputs (sidebar do Streamlit)**:
- String de busca (aceita sintaxe booleana `AND`/`OR` — confirmado empiricamente que é interpretada como operador lógico real por todas as bases, ver seção 8).
- Ano inicial / Ano final.
- Seleção de quais das 6 bases raspar (multiselect).
- Máximo de páginas por base (paginação).
- Checkbox "Buscar resumo completo (mais lento)" — opcional, ver seção 7.
- Checkbox "Modo debug (exibir navegador)" — desliga o modo headless para depuração visual.
- Abaixo do campo de busca, uma **sugestão automática via expressão regular** avalia a qualidade da string digitada e recomenda melhorias, sem bloquear a busca (ver seção 9).

**Output**: DataFrame/CSV consolidado com as colunas: `Base de Origem`, `Título`, `Autores`, `Ano`, `Resumo`, `Link`, `String de Busca` (a string exatamente enviada àquela base — ver seção 8).

## 4. Arquitetura

- **Orquestrador** (`main()`): lê os parâmetros da UI, chama cada scraper selecionado através de um dicionário `scrapers_disponiveis` (nome da base → função), com **assinatura uniforme** em todas as 6 funções de scraper: `(driver, termo_busca, ano_inicio, ano_fim, max_paginas, buscar_resumo)`.
- **Modularidade**: uma função de scraper por base (`scraper_ieee`, `scraper_acm`, `scraper_scopus`, `scraper_rbie`, `scraper_renote`, `scraper_sbie`), conforme requisito original do trabalho. As 3 bases brasileiras (RBIE, RENOTE, SBIE) rodam sobre a mesma plataforma de publicação (OJS/PKP), então parte da lógica é compartilhada em funções auxiliares privadas (`_scraper_ojs_generico`, `_extrair_artigos_ojs`) para evitar duplicação — mas cada base continua tendo sua própria função pública chamada individualmente pelo orquestrador.
- **Resiliência (requisito central do trabalho)**: cada função de scraper tem seu próprio `try/except` interno, captura qualquer exceção, reporta o erro na interface via `st.error`/`st.warning`, e retorna uma lista parcial (mesmo vazia) — nunca propaga a exceção. O orquestrador tem uma segunda camada de `try/except` em volta de cada chamada, como defesa extra. **Isso foi comprovado experimentalmente**: numa coleta com as 3 bases internacionais marcadas, a falha da ACM (bloqueio Cloudflare) gerou uma mensagem de erro na tela mas não impediu a coleta de prosseguir normalmente para a Scopus.
- **Modo Stealth (anti-bot)**: `configurar_driver()` configura o Chrome com: modo headless (ver nota importante abaixo), User-Agent real de Windows/Chrome (sobrescrito manualmente), remoção das flags de automação (`excludeSwitches`, `useAutomationExtension`, `disable-blink-features=AutomationControlled`), remoção de `navigator.webdriver` via comando CDP, e `--ignore-certificate-errors` para redes institucionais com proxy.
- **Espera inteligente**: uso de `WebDriverWait` + `expected_conditions` em vez de `time.sleep()` fixo para aguardar elementos do DOM. `time.sleep()` só é usado deliberadamente como pausa "educada" entre requisições de paginação (anti-rate-limiting), não para esperar carregamento.

### Nota importante sobre o modo headless

Durante os testes, descobriu-se um bug real do Chromium no Windows: o modo `--headless=new` (mais recente) fazia uma janela em branco continuar visível durante toda a coleta, mesmo removendo `--disable-gpu`. A solução foi usar o modo headless "clássico" (`--headless`), mais antigo porém comprovadamente estável no Windows — sua desvantagem teórica (User-Agent contendo "HeadlessChrome", mais fácil de detectar) já é neutralizada porque o código sobrescreve o User-Agent manualmente de qualquer forma.

## 5. As 6 bases: implementação e limitações reais de cada uma

Cada base tem um comportamento **diferente e documentado**, resultado de testes reais contra os sites (não é comportamento hipotético/idealizado):

### IEEE Xplore — funciona bem
- Estratégia: em vez de raspar o HTML renderizado (frágil, pois é uma SPA Angular), a função `_buscar_ieee_via_api()` chama diretamente a **API REST interna** que o próprio site usa (`POST /rest/search`), executada via `driver.execute_async_script` (fetch dentro do contexto da página, reaproveitando os cookies de sessão).
- Isso resolve paginação de forma trivial (parâmetro `pageNumber`) e traz o resumo completo direto da API, sem custo extra.
- Tem fallback: se a API mudar/falhar, cai para raspagem tradicional de HTML da 1ª página (`_scraper_ieee_dom_fallback`).
- **Limitação conhecida**: é um endpoint não documentado publicamente (engenharia reversa do comportamento do site), sujeito a mudar.
- Busca em português retorna 0 resultados genuinamente (não é bug — IEEE indexa majoritariamente conteúdo em inglês).

### ACM Digital Library — bloqueada por anti-bot (limitação documentada, não um bug)
- A ACM usa proteção Cloudflare de nível enterprise (Cloudflare Turnstile / "Executando verificação de segurança"), que o modo stealth implementado não consegue contornar.
- Comprovado visualmente em teste real (print da tela de verificação do Cloudflare).
- Isso é tratado como uma limitação válida a discutir no TCC (prova que o stealth funciona contra proteção leve/moderada como a do IEEE, mas não contra proteção enterprise).

### Scopus (Elsevier) — exige login institucional (limitação documentada)
- Comprovado visualmente: a Scopus redireciona para uma tela de login (SSO institucional / Elsevier ID) antes de mostrar qualquer resultado de busca.
- Tratado como limitação, com alternativa sugerida no código (Scopus Search API oficial, via `dev.elsevier.com`, fora do escopo implementado).
- É a única base cuja string de busca enviada é diferente da digitada pelo usuário: é envelopada em `TITLE-ABS-KEY(<termo>)`, sintaxe oficial documentada da Scopus (busca em título, resumo e palavras-chave).

### RBIE (Revista Brasileira de Informática na Educação) — funciona, com solução não-óbvia
- **Descoberta importante feita durante os testes**: a busca nativa do OJS da RBIE (`.../rbie/search/search`) retorna sempre "Nenhum resultado encontrado", mesmo usando o formulário oficial do próprio site manualmente, com termos genéricos. Não é um bug do scraper — é uma limitação/falha do lado da RBIE (provavelmente um índice de busca vazio ou quebrado após a migração da revista para essa plataforma em 2021).
- **Solução encontrada**: a SBC disponibiliza uma ferramenta de busca **separada e unificada** entre todas as suas publicações, a "Busca Integrada" da SOL (SBC Open Library, `sol.sbc.org.br/busca/`), que de fato indexa o conteúdo da RBIE. O `scraper_rbie()` usa esse endpoint alternativo — pesquisando no campo "Qualquer lugar" (parâmetro `query`) e marcando apenas a categoria "Periódicos" (parâmetro `archiveIds[]=2`) — filtrando os resultados apenas para links da RBIE (já que essa busca é cruzada entre várias publicações da SOL).
- Extração de autores/ano usa uma técnica de navegação pelo DOM (`_texto_ate_proximo_link`) que captura o texto entre o link de um artigo e o próximo (necessário porque essa página específica não separa os resultados em containers HTML individuais).
- **Decisão de escopo confirmada com o orientador**: a SBIE também é hospedada na plataforma SOL (categoria "Anais de Eventos", não "Periódicos"), mas **não** foi migrada para esse mesmo mecanismo de Busca Integrada, pois a busca nativa dela já funciona corretamente — a Busca Integrada foi adotada apenas como solução pontual para o problema específico da RBIE, não como padrão geral.
- **Limitação conhecida**: paginação desse endpoint específico não foi confirmada (os testes sempre couberam em uma única página de resultados).

### RENOTE (Revista Novas Tecnologias na Educação) — funciona bem
- Usa a busca nativa do OJS/SEER da UFRGS normalmente, através da lógica genérica compartilhada (`_scraper_ojs_generico`).
- Paginação via parâmetro de URL `searchPage=N` (confirmado nos testes), sem necessidade de clique em botão.

### SBIE (Anais do Simpósio Brasileiro de Informática na Educação) — funciona bem
- Mesma lógica genérica de OJS da RENOTE, usando a busca nativa da plataforma SOL (não a Busca Integrada — ver decisão de escopo acima).
- Paginação também via `searchPage=N`.

## 6. Correção de dados: eliminação automática de valores "N/A" em Autores/Ano

Durante os testes, artigos das 3 bases OJS ocasionalmente vinham com "N/A" nas colunas Autores e/ou Ano. Investigação confirmou que isso era falha de uma heurística de extração (texto ao redor do link do artigo na listagem), não ausência real do dado — todo artigo publicado tem autor e ano, então "N/A" só podia significar falha de extração.

**Solução implementada**: a página de detalhe de qualquer artigo OJS expõe metadados de citação padronizados via tags HTML `<meta name="citation_author">` e `<meta name="citation_date">` (o mesmo padrão lido pelo Google Scholar). A função `_enriquecer_via_detalhe_ojs()` visita a página de detalhe **automaticamente**, sem exigir nenhuma opção na interface, mas **somente quando necessário** (Autores ou Ano vieram "N/A", ou o checkbox de resumo está ativo) — artigos que já vêm completos da listagem não geram nenhuma requisição extra. Essa correção foi validada e eliminou 100% dos casos de N/A nos testes realizados (0 casos de N/A em Autores/Ano em SBIE, RBIE e RENOTE após o ajuste, testado com o termo "ensino" no período 2018–2026).

## 7. Funcionalidade de resumo completo (bases OJS)

Diferente do IEEE/ACM/Scopus (onde o resumo já vem na própria listagem de busca), o OJS só exibe o resumo completo na página de **detalhe** de cada artigo, não na listagem. Buscar isso custaria uma requisição HTTP extra por artigo.

Decisão tomada (após avaliar 3 alternativas com o aluno): checkbox opcional "Buscar resumo completo (mais lento)" na sidebar, desmarcado por padrão. Quando ativado, o scraper visita a página de detalhe de cada artigo para extrair o resumo de `div.item.abstract`.

**Importante**: foi avaliada e **descartada** a alternativa de usar o protocolo OAI-PMH (que o OJS também expõe publicamente e que devolveria o resumo "de graça", sem requisição extra por artigo, similar à API do IEEE) — porque o OAI-PMH não suporta busca por palavra-chave/booleana, apenas "download" de tudo por intervalo de data. Adotar essa alternativa quebraria a busca booleana (`AND`/`OR`) da ferramenta e inverteria o princípio arquitetural de que a relevância da busca deve ser decidida pelo motor de busca nativo de cada base, não por um filtro de texto local — o que criaria inconsistência entre as 6 bases. Essa decisão e sua justificativa estão registradas como parte da metodologia/discussão do trabalho.

## 8. Investigação empírica do suporte a AND/OR e a coluna "String de Busca"

O orientador solicitou uma melhoria na forma como a string de busca (com operadores `AND`/`OR`) é tratada por cada base, pedindo que a ferramenta seguisse "à risca" a sintaxe de busca real de cada uma. Antes de implementar qualquer transformação, foi feito um **levantamento empírico** (não teórico) de como cada base testável de fato interpreta esses operadores, comparando a quantidade de resultados/páginas retornadas para os mesmos termos combinados de formas diferentes.

**Metodologia do teste**: para cada base, comparou-se o resultado de: termo A sozinho; termo B sozinho; "A AND B"; "A OR B"; "A B" (sem operador); e uma frase entre aspas ("A e B"). Nos casos em que a página de resultados limita a exibição (25 por página), a comparação foi feita pela quantidade de PÁGINAS de paginação disponíveis, não apenas pela contagem da primeira página.

**Resultados**:
- **RENOTE** (termos "ensino" e "robotica"): "ensino" = 55 resultados; "robotica" = 54; "ensino AND robotica" = 27 (interseção, menor que os dois — AND funcionando como operador lógico real); "ensino OR robotica" = 55 (união, igual ao maior dos dois — OR correto); frase entre aspas "ensino de robotica" = 8 (busca de frase exata, mais restritiva).
- **SBIE e RBIE (via Busca Integrada)** (termos "ensino" e "aprendizagem", escolhidos por ambos terem resultado individualmente): "ensino AND aprendizagem" não gerou nenhuma página adicional de paginação (poucos resultados, coerente com interseção real); já "ensino OR aprendizagem" e "aprendizagem" sozinho geraram paginação até a página 10 (muitos resultados, coerente com união real).
- **IEEE Xplore**: validado anteriormente com a string "machine learning AND web scraping", retornando resultados topicamente coerentes com ambos os termos.
- **ACM e Scopus**: não foi possível testar empiricamente (bloqueadas por Cloudflare e login institucional, respectivamente — ver seção 5), mas ambas têm sintaxe de busca booleana oficialmente documentada pelos próprios fornecedores.

**Conclusão e decisão tomada**: todas as bases testáveis já interpretam corretamente `AND`/`OR` em maiúsculas e frases entre aspas — **não havia necessidade de nenhuma transformação de sintaxe**. A string do usuário já é enviada seguindo o formato nativo esperado por cada base. A única exceção é a Scopus, cuja sintaxe oficial exige o envelopamento em `TITLE-ABS-KEY(...)` (já implementado desde a criação do projeto).

**Ajuste concreto implementado**: como não havia transformação a fazer, o ajuste foi passar a capturar e expor, para cada resultado, a **string exatamente enviada** àquela base — nova coluna `String de Busca` no CSV/DataFrame de saída (idêntica ao termo digitado pelo usuário na maioria das bases; igual a `TITLE-ABS-KEY(<termo>)` no caso da Scopus). Isso atende a um requisito importante de rastreabilidade metodológica de uma RSL: documentar exatamente o que foi buscado em cada fonte.

## 9. Sugestão de melhoria da busca via expressão regular

Também a pedido do orientador, foi criada a função `sugerir_melhoria_busca()`, que analisa a string digitada pelo usuário com heurísticas baseadas em expressões regulares e exibe uma dica (não-bloqueante) logo abaixo do campo de busca na interface. As heurísticas implementadas:

- Aspas desbalanceadas (quantidade ímpar de `"`) → alerta para revisar frases entre aspas.
- Parênteses desbalanceados → alerta para conferir a quantidade de `(` e `)`.
- Uso de `and`/`or` em minúsculas → sugere usar maiúsculas (formato reconhecido como operador pelas bases, conforme validado na seção 8).
- Várias palavras sem nenhum operador `AND`/`OR` → sugere combinar conceitos com `AND` ou sinônimos com `OR`.
- Termo único e muito curto (menos de 5 caracteres) → sugere ser mais específico.

**Casos de teste validados na interface real pelo aluno** (a dica aparece e desaparece automaticamente a cada tecla digitada, comportamento nativo do Streamlit):

| String de busca digitada | Sugestão observada |
|---|---|
| `terapia AND robótica` | Nenhuma (string bem formada) |
| `terapia and robótica` | Usar AND/OR em maiúsculas |
| `terapia AND "robô terapêutico` | Aspas desbalanceadas |
| `terapia (AND robótica` | Parênteses desbalanceados |
| `terapia robótica` | Várias palavras sem operador AND/OR |
| `IA` | Termo único e muito curto |
| `terapia AND robótica OR reabilitação` | Nenhuma (string bem formada) |
| (campo vazio) | Nenhuma |

## 10. Metodologia de testes empregada

Todos os ajustes acima não foram teóricos — foram descobertos e validados através de:
1. Scripts de teste isolados (fora da interface Streamlit) chamando os scrapers diretamente contra os sites reais, incluindo testes comparativos de contagem de resultados/páginas para validar semântica de AND/OR.
2. Testes manuais na interface Streamlit pelo aluno, com prints de evidência.
3. Um documento Word separado (`EVIDENCIA DE TESTES SCRAPING_Luiz Henrique Martini de Souza.docx`) que documenta cronologicamente cada problema encontrado, sua causa raiz, a solução aplicada (com trechos de código antes/depois) e a validação realizada — inclusive quando o resultado foi uma limitação externa (ACM, Scopus, busca nativa da RBIE) e não um bug corrigível, e inclusive as decisões de escopo tomadas junto ao orientador.

## 11. O que isso significa para o texto do TCC

Pontos que este software sustenta e que podem virar argumentos no texto:
- A ferramenta resolve de fato o gargalo de coleta manual multi-base, com ganho de tempo mensurável (paginação automatizada, sem necessidade de copiar/colar campo a campo).
- A arquitetura de resiliência (requisito central do trabalho) foi comprovada experimentalmente, não é apenas teórica.
- As limitações de ACM e Scopus **não são falhas de implementação** — são evidências empíricas válidas sobre os limites reais de scraping contra proteção anti-bot enterprise e sistemas de autenticação institucional, um achado legítimo para a seção de limitações/discussão do TCC.
- A ferramenta delibera e documenta suas decisões de escopo (ex.: não fazer filtro de relevância local, preferir a busca nativa de cada base, não migrar a SBIE para a Busca Integrada) com base em justificativa metodológica ligada a frameworks de RSL como o PRISMA e em validação empírica (não em suposição) — isso é defensável academicamente, não é uma limitação técnica escondida.
- A investigação sobre AND/OR (seção 8) é um bom exemplo de método científico aplicado ao desenvolvimento: hipótese, teste controlado, comparação de resultados, e uma conclusão que evitou uma mudança de código desnecessária — vale destacar isso na metodologia do TCC, não só o resultado.
- Diversos problemas reais de engenharia foram enfrentados e resolvidos (bug de paginação por seletor desatualizado, bug de janela headless do Chromium, bug de extração por heurística de texto, bug de metadados incompletos), documentados com causa raiz, solução e validação — bom material para a seção de desenvolvimento/resultados do trabalho.
- A coluna "String de Busca" e a sugestão via regex reforçam o compromisso do trabalho com rastreabilidade metodológica e usabilidade, características valorizadas em ferramentas de apoio à pesquisa científica.

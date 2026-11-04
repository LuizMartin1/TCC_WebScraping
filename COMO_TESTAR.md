# Como rodar a ferramenta (passo a passo)

## Pré-requisitos

- **Python** instalado (versão 3.10 ou mais recente). Para verificar, abra o terminal/PowerShell e digite:
  ```
  python --version
  ```
  Se não aparecer um número de versão, baixe em https://www.python.org/downloads/ (marque a opção "Add Python to PATH" durante a instalação).

- **Google Chrome** instalado (o mais recente, qualquer versão atual serve).

## Passo a passo

1. Abra o terminal (PowerShell, no Windows) dentro da pasta do projeto (a pasta que contém o arquivo `app.py`).

2. Instale as dependências (só precisa fazer isso uma vez):
   ```
   pip install -r requirements.txt
   ```

3. Rode a ferramenta:
   ```
   python -m streamlit run app.py
   ```

4. Isso deve abrir automaticamente uma aba no navegador em `http://localhost:8501`. Se não abrir sozinho, copie esse endereço e cole no navegador.

## Usando a ferramenta

Na barra lateral esquerda:

1. Digite a **string de busca** (ex.: `"machine learning" AND "web scraping"`). Uma dica pode aparecer abaixo do campo sugerindo melhorias na string.
2. Defina o **ano inicial** e **ano final**.
3. Marque quais das **6 bases** você quer pesquisar (IEEE Xplore, ACM Digital Library, Scopus, RBIE, RENOTE, SBIE).
4. Ajuste o **máximo de páginas por base**, se quiser mais resultados (cada página a mais deixa a coleta um pouco mais lenta).
5. Deixe **"Modo debug"** desmarcado (ele é só para eu depurar seletores quebrados — abre uma janela do Chrome visível).
6. Clique em **"Iniciar coleta"** e aguarde. Cada base mostra uma mensagem de status conforme vai sendo processada.
7. Ao final, os resultados aparecem numa tabela na tela, e há um botão **"Baixar CSV consolidado"** para exportar tudo.

## O que esperar

- **IEEE Xplore, RENOTE e SBIE**: devem retornar resultados normalmente.
- **RBIE**: retorna resultados através de um mecanismo alternativo de busca (mais detalhes no material de evidências de testes).
- **ACM Digital Library**: normalmente bloqueada por proteção anti-bot do site (Cloudflare) — a ferramenta mostra um erro na tela para essa base, mas continua coletando as outras normalmente. Isso é uma limitação documentada do site, não um problema da ferramenta.
- **Scopus**: exige login institucional para mostrar resultados — também tratado como limitação documentada.

## Para encerrar

No terminal onde a ferramenta está rodando, aperte `Ctrl+C`.

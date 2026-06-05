# Credenciais ligadas ao site de origem

**Status:** approved (Pedro, 2026-06-05)
**Branch:** `feat/cred-site-link` (worktree off `origin/main`)

## Problema

Hoje `jp` guarda credenciais nomeadas (`credentials.json` → `{name: {token_path}}`)
sem saber a que servidor cada token pertence. Quando há várias credenciais e o
usuário roda um comando ligado a uma URL (`jp live <URL>`, `jp terminal <URL>`,
`jp clone <URL>`), o picker mostra **todas** indiscriminadamente.

## Objetivo

Cada credencial passa a guardar o **site de origem** (origin `scheme://host`).
Quando o usuário mexe com uma URL, `jp` computa o origin do alvo e:

- **1 credencial** para aquele site → usa automaticamente, sem perguntar.
- **>1** → tela de seleção só com as daquele site, com:
  - shortcut que alterna pra mostrar **todas** as credenciais;
  - ao lado de cada nome, o **site** a que pertence;
  - credencial **sem site** (legado) aparece marcada `(sem site)`; com ela
    destacada, um shortcut deixa o usuário colar a URL e preencher o site na
    hora — depois ela some ou permanece no filtro conforme o site do alvo.

## Modelo de match

Origin = `scheme://host[:porta]`, lowercase, sem path nem barra final. Token
pertence a um hub inteiro, então URLs de `/user/alice` e `/user/bob` no mesmo
host casam a mesma credencial.

## Contrato de interfaces (os subagents seguem isto à risca)

### `src/jp/urls.py` (novas funções)

```python
def origin_of(url: str) -> str:
    """scheme://host[:porta] em lowercase, sem path/barra. "" se vazio/não http(s)."""

def username_of(url: str) -> str:
    """Segmento após /user/ numa URL JupyterHub, ou "" se ausente."""
```

### `src/jp/credentials.py`

- `Credential` dataclass ganha `site: str = ""`.
- `_entries` lê `entry.get("site", "")`.
- Registry grava a chave `"site"` **apenas quando não-vazia** (mantém limpo).
- `add(name, token, *, scope, root=None, overwrite=False, site="")` — grava o site.
- `add_path(name, token_path, *, scope, root=None, overwrite=False, site="")`.
- `set_site(name, site, *, scope, root=None) -> Credential` — atualiza o `site`
  na registry do escopo certo (global/local); `UsageError` se o nome não existe
  naquele escopo.
- `list_for_site(site, root=None) -> list[Credential]` — credenciais com
  `cred.site == site` **ou** `cred.site == ""` (legado é wildcard). `site == ""`
  → devolve tudo (`list_credentials`). Comparação case-insensitive no origin.

### `src/jp/commands/_context.py`

`resolve_credential(*, credential="", token_path="", root=None, target_url="")`:

1. `token_path` explícito → `""`.
2. `credential` explícito → valida contra `list_credentials(root)` (todas) → retorna.
3. `site = urls.origin_of(target_url)` se houver `target_url`.
4. `available = list_credentials(root)`. Se vazio → checa `JP_TOKEN`/`JP_TOKEN_FILE`
   (retorna `""`) senão `AuthError` (mensagem atual).
5. `pool = list_for_site(site, root)` se `site` senão `available`. Se `pool`
   vazio (site não casa nada) → `pool = available` (fallback: mostra todas).
6. `len(pool) == 1` → usa silenciosa.
7. `>1`: interativo → `tui.select_credential(available, target_site=site,
   on_set_site=...)` (TUI filtra/alterna internamente). Não-interativo →
   `UsageError` listando nomes de `pool`.

`on_set_site(cred, raw_url) -> str`: faz `origin_of(raw_url)`, persiste via
`credentials.set_site(cred.name, origin, scope=cred.scope, root=root)`, devolve o
origin gravado (`""` se URL inválida — TUI não muda nada nesse caso).

- `choose_credential` passa `target_url=getattr(args, "url", "")`.
- `config_from_url` passa `target_url=base_url` (origin_of corta o path).

### `src/jp/tui.py`

```python
def select_credential(creds, target_site="", on_set_site=None, title="",
                      _reader=None):
    """creds: lista de objetos com .name/.scope/.site. Devolve o objeto escolhido
    ou None. Dois modos: filtrado (site==target_site ou site=="") e todas; 'a'
    alterna. 's' na linha destacada sem site abre input inline pra colar URL ->
    on_set_site(cred, raw) -> str; muta cred.site com o retorno. Setas movem,
    Enter escolhe, Esc cancela. _reader é test seam (igual select_one)."""
```

Linha: `> nome  (scope)  <site ou (sem site)>`. Input inline reusa
`reader.read_key()` acumulando string até enter/esc. Rodapé mostra os atalhos
ativos (`a todas/site · s definir site` só quando aplicável).

### `src/jp/commands/login.py` (fluxo reordenado)

1. `find_root()`.
2. **Site primeiro** — `_ask_site(args)`: cola URL (`--url`/`--site` non-interativo;
   vazio permitido). `parse_clone_url` → base_url; `origin = origin_of(base_url)`;
   `user = username_of(base_url)`. Retorna `(origin, user)`.
3. **Nome com default** — `_ask_name(args, default)`: default sugerido
   `f"{user}-{host}"` se user, senão `host`, senão `""`; `host` = origin sem
   scheme; sanitizado pro alfabeto de `validate_name`. Prompt
   `Name [<default>]: `, Enter aceita o default.
4. `_ask_scope` (igual).
5. **Browser** — se `origin` e tty e não `--no-browser`: mostra
   `<origin>/hub/token`, confirma, `webbrowser.open(...)`. Sem tty → só imprime.
6. Adquire token (paste hidden) ou `--token-path` (igual).
7. `add(..., site=origin)` / `add_path(..., site=origin)`.
8. Grava credential no config do workspace (igual).

Args novos: `--url`/`--site` (URL non-interativa), `--no-browser`.

## Backwards-compat

- Credenciais sem `site` continuam válidas; viram wildcard no filtro.
- `resolve_credential` sem `target_url` → comportamento atual (todas + `select_one`).
- Registry sem `site` carrega normal (`entry.get`).

## Testes

- `tests/test_credentials.py`: site persiste, `list_for_site` (match + wildcard),
  `set_site` (escopo certo, nome ausente), `add`/`add_path` com site.
- `tests/test_urls.py` (ou existente): `origin_of`, `username_of`.
- `tests/test_config_from_url.py` + `tests/test_clone_init.py`: filtro por origin,
  1-match auto, fallback todas, `target_url` threading.
- `tests/test_tui_select_credential.py`: reader seam — toggle `a`, editar site `s`
  (relista), escolher.
- `tests/test_login.py`: mock `webbrowser`+stdin — site salvo, nome default,
  `--no-browser`, `--url`.

## Ferramentas

Testes: `PYTHONPATH=src /Users/pedro/pepy/bin/pytest -q`. Lint:
`/Users/pedro/pepy/bin/ruff check src tests` + `ruff format --check`.

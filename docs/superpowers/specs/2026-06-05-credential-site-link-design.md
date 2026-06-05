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

**Independente da jp-live**: a feature mexe SÓ no corpo de `choose_credential`
(mesma assinatura que `origin/main`). Não adiciona `resolve_credential` nem
`config_from_url` — essas são da branch `feat/jp-live-mount`; recriá-las aqui só
geraria conflito sem caller nesta branch. Quando a jp-live mergear, ela passa o
`target_url=base_url` pro seu próprio fluxo.

`choose_credential(args, root=None)` (lógica inline, ganho de filtro por site):

1. `args.token_path` → `""`.
2. `args.credential` explícito → valida contra `list_credentials(root)` → retorna.
3. `available = list_credentials(root)`. Se vazio → checa `JP_TOKEN`/`JP_TOKEN_FILE`
   (retorna `""`) senão `AuthError` (mensagem atual).
4. `site = urls.origin_of(args.url)` se houver `args.url`.
5. `pool = list_for_site(site, root)` se `site` senão `available`. Se `pool`
   vazio (site não casa nada) → `pool = available` (fallback: mostra todas).
6. `len(pool) == 1` → usa silenciosa.
7. `>1`: interativo → `tui.select_credential(available, target_site=site,
   on_set_site=...)` (TUI filtra/alterna internamente). Não-interativo →
   `UsageError` listando nomes de `pool`.

`on_set_site(cred, raw_url) -> str`: faz `origin_of(raw_url)`, persiste via
`credentials.set_site(cred.name, origin, scope=cred.scope, root=root)`, devolve o
origin gravado (`""` se URL inválida — TUI não muda nada nesse caso).

`clone`/`init` já passam `args.url` (positional da URL) → filtro automático.

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
ativos. **`s` (definir site) vale pra QUALQUER credencial destacada** (adicionar
ou corrigir o site quando o usuário achar necessário), não só as sem-site —
disponível sempre que `on_set_site` é passado.

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

## Backwards-compat (verificado por smoke test)

- Credenciais sem `site` continuam válidas; viram wildcard no filtro
  (`list_for_site` inclui sempre as sem-site).
- **1 credencial existente → auto-selecionada sem prompt**, mesmo com `target_url`
  (zero mudança de UX pra quem já usa uma só).
- `choose_credential` sem `args.url` → comportamento atual.
- Registry sem `site` carrega normal (`entry.get("site", "")`).
- `jp login` scripted non-tty (sem `--url`) → `site=""`, não lê stdin no passo de
  site; token via stdin/`--token-path` intacto.
- Config de workspace e `load_token` inalterados.

## Testes

- `tests/test_credentials.py`: site persiste, `list_for_site` (match + wildcard),
  `set_site` (escopo certo, nome ausente), `add`/`add_path` com site.
- `tests/test_urls.py` (ou existente): `origin_of`, `username_of`.
- `tests/test_clone_init.py`: `choose_credential` filtra por origin via `args.url`,
  1-match auto, fallback todas, sem-url = legado.
- `tests/test_tui_select_credential.py`: reader seam — toggle `a`, editar site `s`
  (relista), escolher.
- `tests/test_login.py`: mock `webbrowser`+stdin — site salvo, nome default,
  `--no-browser`, `--url`.

## Incremento: gerenciar credenciais (`jp credentials`)

Não havia como ver/editar/deletar credenciais salvas (só `jp login` adiciona).
Decisão (Pedro): **comando dedicado** (não shortcut destrutivo no picker do clone)
+ hint. Delete nunca no picker — só `s` (set-site, não-destrutivo) lá.

- `credentials.py`: `remove(name, *, scope, root=None)` (apaga entry; unlink do
  token file só se gerenciado em `credentials.d` — externos via `add_path` ficam),
  `rename(old, new, *, scope, root=None)` (valida nome, renomeia entry + token
  gerenciado).
- `tui.py`: `credential_manager(creds, *, on_delete, on_set_site, on_rename)` —
  TUI: setas movem, `d` deleta (confirma `[y/N]`), `s` site, `r` renomeia, `q`
  sai. `select_credential` ganha linha hint `Manage saved credentials with: jp
  credentials`.
- `commands/credentials_cmd.py`: sem args + tty → manager interativo; flags pra
  script `--list` / `--rm NAME` / `--rename OLD NEW` / `--set-site NAME URL` /
  `--force`. `--rm` non-tty exige `--force`. Registrado em `commands/__init__.py`.

## Ferramentas

Testes: `PYTHONPATH=src /Users/pedro/pepy/bin/pytest -q`. Lint:
`/Users/pedro/pepy/bin/ruff check src tests` + `ruff format --check`.

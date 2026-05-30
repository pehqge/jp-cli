# jp — Design consolidado (fonte da verdade da implementação)

> CLI git-like para sincronizar pastas locais com um servidor **JupyterHub** remoto via a
> **Contents REST API**, e (fase 2) rodar código no kernel remoto. Stdlib-puro, zero
> dependências. Foco absoluto: **nunca destruir dados** numa máquina compartilhada.
>
> Este documento é normativo: a implementação DEVE segui-lo. Derivado de 7 dimensões de
> pesquisa em `research/` + sondagem empírica do servidor real (jupyter_server 2.14.1).

---

## 0. Princípios inegociáveis

1. **Zero dependências externas.** Só stdlib (`urllib`, `ssl`, `json`, `base64`, `hashlib`,
   `argparse`, `configparser`, `getpass`, `posixpath`, `unicodedata`). Roda em qualquer
   Python ≥ 3.8 em Mac/Win/Linux.
2. **Nunca deletar remoto por padrão.** `push`/`pull` só criam/atualizam. Delete é comando
   separado, gated, com dry-run + confirmação digitada.
3. **Nunca "last writer wins" silencioso.** Conflito 3-way (base≠local E base≠remoto) →
   aborta aquele arquivo e reporta. Sobrescrever divergência remota exige `--force`.
4. **Path-jail nos dois sentidos.** Toda op remota confinada ao prefixo do clone; todo write
   local confinado ao clone root (servidor é input não-confiável → anti Zip-Slip).
5. **Token nunca toca disco versionado nem logs.** Só o *caminho* do arquivo de token fica na
   config. `redact()` central em toda saída.
6. **Resiliência por-arquivo.** Uma falha benigna (ex.: dotfile recusado) não aborta o run;
   coleta, continua, resume no fim com exit code correto.
7. **Recusar a raiz compartilhada.** Prefixo vazio/`/`/`.`/`~`/pasta protegida → recusado.

---

## 1. Fatos empíricos do servidor (jupyter_server 2.14.1, UFSC) — confirmados ao vivo

| Operação | Comportamento real | HTTP |
|---|---|---|
| `GET /api/status` | `{"version":"2.14.1"}` via `/api/` | 200 |
| `PUT` dir (`{"type":"directory"}`) | cria | **201** |
| `PUT` file normal | cria/sobrescreve **incondicional** | **201** |
| `PUT` **dotfile** (`.gitignore`) | recusado `allow_hidden=False` | **400** |
| `PUT` dir oculto | recusado | **400** |
| `PATCH` rename → nome oculto | recusado | **400** |
| `DELETE` arquivo | remove, sem trash | **204** |
| `DELETE` dir **vazio** | remove | **204** |
| `DELETE` dir **NÃO-vazio** | **recusado** (não é recursivo!) | **400** |
| `GET ?hidden=1` | servidor **não** lista dotfiles | 200 |
| `GET` path inexistente | `{"message":...,"reason":...}` | 404 |
| campos do model | `name,path,type,size,last_modified,created,writable,hash,hash_algorithm,mimetype,format,content` | — |
| `hash`/`hash_algorithm` sem param | **`null`** (não computado por padrão) | — |
| **`GET ?hash=1`** | retorna **sha256** real (== `shasum -a 256` local), funciona com **`content=0`** (hash SEM baixar o body; 20 MiB em ~0.11s) | 200 |
| PUT arquivo novo / overwrite | **201** novo · **200** sobrescreve (sinal create-vs-update) | |
| PUT `a/b/c/x` sem pais | **auto-cria pais** (mkdir -p no PUT) | 201 |
| `PATCH` move p/ pai inexistente | **NÃO cria pais** → erro | 500 |
| `PATCH` move p/ alvo existente | **não sobrescreve** | 409 |
| `POST {copy_from}` | cópia server-side (sem transferir) | 201 |
| `created` (campo) | **não-confiável** (muda no overwrite/rename) — usar só `last_modified` | |
| `GET` 404 (body) | **TEXTO PLANO**, não JSON → parsing defensivo | 404 |
| `format=text` em binário | retorna **raw bytes/NUL com 200** (não é guard de binário!) | 200 |
| servidor parado (JupyterHub) | **302 → `/hub/...`** (não seguir redirect) | 302 |
| checkpoints | funcionam (1 por arquivo; restore → 204) | |
| JupyterHub | **5.2.1**; root em disco = `/lapix` (vaza em erros) | |
| sem chunking | PUT = 1 base64 em memória (~33% inflação) → limitar arquivos grandes | |

**Implicações de design travadas por estes fatos:**
- **`?hash=1` é o critério primário de mudança** → detecção barata e definitiva: comparar
  `GET <file>?content=0&hash=1` (sha256) com o sha256 local **sem baixar**. `last_modified`+
  `size` viram só pré-filtro do listing (children do listing **não** trazem `hash`/`content`).
  Baixar o body **só** quando os hashes diferem. (Corrige o design anterior que assumia
  hash=null e exigia download pra comparar.)
- **Nested PUT auto-cria pais** → escrever paths profundos numa chamada; `ensure_remote_dirs`
  vira defensivo/opcional. **PATCH não cria pais** (500) → ao mover, criar o dir destino antes.
- **PUT: 201=criou, 200=sobrescreveu** → usar como sinal de create-vs-update no relatório.
- **Sem write condicional** (sem If-Match/ETag) → re-GET `?hash=1` imediatamente antes do PUT
  (mitiga corrida, agora barato).
- **`created` não-confiável** → o index usa só `last_modified` + `hash` para o lado remoto.
- **`format=text` não é guard de binário** → jp decide texto×binário no cliente e usa
  `format=base64` para não-UTF-8; `.ipynb` forçar `type=file&format=text` p/ bytes fiéis
  (detectar notebook por extensão, pois o metadata ainda reporta `type:notebook`).
- **Erros em formatos mistos** (404 texto plano, 403 JSON, 400 JSON…) → parser de erro
  defensivo; mensagens vazam `/lapix/...` → **redact de paths absolutos** no output ao usuário.
- **Health probe:** `GET /api/status` **sem seguir redirects**: 200=up · 403 JSON=token
  ruim · 3xx→`/hub/`=servidor parado · conn error=rede/Hub down.
- **Dotfiles impossíveis** → política `skip` default (ver §8), e **nada que o jp escreve no
  remoto pode começar com `.`** (temporários = `jp-tmp/`, sem ponto).
- **DELETE NÃO é recursivo** (dir não-vazio → 400) → `jp rm --recursive` apaga **bottom-up**
  (folhas→subdirs→dir), nunca confiar em recursão server-side. `jp rm` ultra-gated (§7).
- ⚠️ **DELETE vai para a LIXEIRA, não apaga de verdade** (`delete_to_trash=True` → manda para
  `~/.local/share/Trash/`), e em certos casos **corrompe o listing do diretório pai** (GET do
  pai passa a retornar 400 "is not a directory"), recuperável só via kernel/terminal. Reforça a
  regra de ouro: **jp NUNCA deleta no remoto em sync normal**; `jp rm` avisa do efeito-lixeira e
  é o único deleter. `status`/`pull`/`push` devem **degradar com aviso** se um GET de diretório
  vier 400 (estado pré-corrompido por terceiros), nunca quebrar.
- **PATCH (rename/move) é mais seguro que DELETE**: move atômico (200), recusa overwrite (409),
  **não** gera lixeira. Preferir PATCH a DELETE quando precisar mover.
- **Sem chunking / ~33% inflação base64** → timeouts generosos+configuráveis, barra de
  progresso, e aviso/limite para arquivos muito grandes (~>50–100 MB).

---

## 2. Layout do repositório (src-layout, hatchling)

```
jp/
├── pyproject.toml            # hatchling, src-layout, [project.scripts] jp=jp.cli:main, deps=[]
├── README.md                 # hero, install por-OS, quickstart, comandos, segurança, FAQ
├── LICENSE                   # MIT
├── CHANGELOG.md              # Keep a Changelog
├── CONTRIBUTING.md
├── SECURITY.md               # como reportar + postura "nunca deleta remoto"
├── CODE_OF_CONDUCT.md
├── .gitignore                # Python + .jp/ + tokens + .env
├── .editorconfig
├── .pre-commit-config.yaml   # ruff + ruff-format
├── src/jp/
│   ├── __init__.py           # __version__ (single source) + fallback importlib.metadata
│   ├── __main__.py           # python -m jp / alvo do zipapp
│   ├── cli.py                # argparse, subparsers, set_defaults(func), dispatch, top-level except
│   ├── errors.py             # JpError + subclasses com exit_code
│   ├── ui.py                 # cor (NO_COLOR/isatty/Win VT), progress, prompts, info/warn/err, redact
│   ├── config.py             # INI (.jp/config + global XDG), precedência, resolução de token
│   ├── paths.py              # find_root, normalize_rel, remote_path_for, assert_within_prefix,
│   │                         #   safe_local_dest, atomic_write  (TODA validação de caminho)
│   ├── api.py                # cliente urllib: Contents API, retry/backoff, timeouts, TLS, redact
│   ├── index.py              # .jp/index (estado-base): load/save, entradas, sha256
│   ├── ignore.py             # ignores embutidos + .jpignore (fnmatch), política de dotfiles
│   ├── sync.py               # diff de árvore, classificação 3-way, plano pull/push, dry-run
│   ├── kernel.py             # (fase 2) sessions/kernels REST + ws.py
│   ├── ws.py                 # (fase 2) mini cliente WebSocket RFC6455 stdlib
│   └── commands/
│       ├── __init__.py
│       ├── clone.py  init.py  login.py  pull.py  push.py  status.py
│       ├── ls.py     diff.py  config_cmd.py  ignore_cmd.py  doctor.py  version.py
│       └── rm.py     (gated)  run.py kernel.py (fase 2)
├── tests/
│   ├── conftest.py           # fixtures: FakeApi (mock urllib), temp repo, tripwire HOME
│   ├── test_paths.py         # INV de path-jail (os dois sentidos) — P0
│   ├── test_sync.py          # tabela 3-way, no-delete, no-overwrite-on-conflict — P0
│   ├── test_security.py      # 20 invariantes (traversal, token, dry-run, atomic) — P0/P1
│   ├── test_config.py  test_ignore.py  test_api.py  test_cli.py
│   └── test_dotfiles.py      # skip default, resiliência, não-aborta
├── scripts/
│   ├── install.sh            # curl|sh  (Mac/Linux)
│   └── install.ps1           # irm|iex  (Windows)
├── docs/
│   ├── architecture.md  security.md  commands.md
└── .github/
    ├── workflows/ci.yml      # matriz OS×Py (ubuntu/macos/windows, 3.8–3.13): ruff+mypy+pytest
    ├── workflows/release.yml # tag v* → wheel/sdist/pyz/binários → PyPI OIDC + GH Release
    ├── ISSUE_TEMPLATE/{bug_report.md,feature_request.md}
    ├── PULL_REQUEST_TEMPLATE.md
    └── dependabot.yml
```

---

## 3. Formato de `.jp/` (local, nunca versionado, nunca sobe ao remoto)

```
.jp/
├── config      # INI: [remote] url/host/api_base/prefix/tokenfile/readonly/server_allows_hidden
├── index       # JSON: estado-base por arquivo {local_size,local_mtime_ns,local_sha256,
│               #        remote_last_modified,remote_size,synced_at}
├── REMOTE      # txt: host+prefix p/ check-access (marcador anti "remoto sumiu")
└── log         # JSONL append-only: ts, op, remote_path, local_path, bytes, result (SEM token)
```

`.jp/config` exemplo (INI, estilo git):
```ini
[remote]
    url = https://jupyter.vlab.ufsc.br/user/pedro.gimenez/lab/tree/privado
    host = https://jupyter.vlab.ufsc.br
    apibase = https://jupyter.vlab.ufsc.br/user/pedro.gimenez
    prefix = privado
    tokenfile = ~/.jupyter_ufsc_token
    readonly = false
    serverallowshidden = false
```

Global (`~/.config/jp/config` ou `%APPDATA%\jp\config`):
```ini
[defaults]
    tokenfile = ~/.jupyter_ufsc_token
    color = auto
    timeout = 30
```

**Token:** resolução `--token-file` → `$JP_TOKEN_FILE`/`$JUPYTER_TOKEN` → repo → global →
`~/.jupyter_ufsc_token`. Só o caminho; o valor nunca é gravado pela config. Permissões:
recusar token group/world-**writable**; avisar se group/other-**readable**.

---

## 4. `paths.py` — a camada de segurança nº 1 (especificação exata)

```python
class PathUnsafe(JpError): exit_code = 8

PROTECTED_PREFIXES = {"compartilhado", "shared", "common", "public"}   # 1º segmento
ROOT_BLOCKLIST     = {"", "/", ".", "./", "~"}

def normalize_rel(path: str) -> str:
    # POSIX lógico. Unicode NFC (macOS NFD→NFC). '\'→'/'. Resolve '.', rejeita escape via '..'.
    # Rejeita: absoluto, NUL. Retorna rel canônico ('' permitido só p/ a raiz do prefixo).
    ...

def validate_prefix(prefix: str) -> str:
    p = normalize_rel(prefix)
    if prefix.strip() in ROOT_BLOCKLIST or p == "":
        raise PathUnsafe("recusado: prefixo remoto resolve para a raiz compartilhada")
    if p.split("/")[0].lower() in PROTECTED_PREFIXES:
        raise PathUnsafe(f"recusado: pasta protegida/compartilhada '{p.split('/')[0]}'")
    return p

def remote_path_for(rel: str, prefix: str) -> str:
    # ÚNICO ponto que monta path remoto. join + prova de confinamento.
    pref = validate_prefix(prefix); safe = normalize_rel(rel)
    full = posixpath.normpath(f"{pref}/{safe}") if safe else pref
    if full != pref and not full.startswith(pref + "/"):
        raise PathUnsafe(f"path {full!r} escapa o prefixo {pref!r}")
    return full

def assert_within_prefix(full: str, prefix: str) -> None:
    # Chamado IMEDIATAMENTE antes de CADA PUT/PATCH/DELETE (defesa em profundidade).
    pref = validate_prefix(prefix)
    if full != pref and not full.startswith(pref + "/"):
        raise PathUnsafe(f"BLOQUEADO: {full!r} fora do sandbox {pref!r}")

def safe_local_dest(clone_root: str, server_rel: str) -> str:
    # Servidor é HOSTIL. Sanitiza nome vindo do listing antes de escrever local.
    # Rejeita: '/', '\', '..', NUL, absoluto, drive letter (C:), Windows-reserved,
    #          trailing dot/space. Prova commonpath([root, realpath(dest)]) == root.
    # Recusa escrever ATRAVÉS de symlink (dest é symlink → erro).
    ...

def atomic_write(dest: str, data: bytes) -> None:
    # tmp no mesmo dir + fsync + os.replace (atômico). Nunca trunca o original antes.
    ...

def find_root(start=None) -> str:
    # sobe do cwd procurando .jp/ (dir). Para em FS root e em $HOME (defensivo). NotARepo se nada.
    ...
```

**Invariantes (viram testes P0):** I-1 todo write remoto passa por `remote_path_for`+
`assert_within_prefix`; I-2 prefixo-raiz recusado; I-3 `..`/abs/NUL rejeitados, nunca
`startswith(prefix)` sem `+"/"`; I-4 download-containment (Zip-Slip); I-7 não escrever através
de symlink; I-8 atomic write.

---

## 5. `api.py` — cliente HTTP (urllib, stdlib)

- **TLS sempre verificado** (`ssl.create_default_context()`), **nunca** unverified. Recusa
  `http://` quando carrega token. Não segue redirect cross-host (host fixado no config).
- **Token só no header** `Authorization: token <T>` — nunca em query string.
- `redact()` aplicado a toda mensagem de erro/log (substitui o valor do token por `***`).
- **Timeouts** configuráveis (default 30s metadata; maior p/ upload). **Retry com backoff**
  só em idempotentes (GET, PUT-conteúdo) e em {429,500,502,503,504}; respeita `Retry-After`;
  **nunca** retry de DELETE/POST ambíguo.
- Métodos: `get_meta(path)`, `get_content(path)->bytes+meta`, `list_dir(path)`,
  `put_file(path, bytes)`, `put_dir(path)`, `patch_rename(src,dst)`, `delete(path)`,
  `checkpoint(path)`, `whoami()` (`GET /api/me`), `server_version()`.
- Erros HTTP → exceções tipadas: 401/403→`AuthError(4)`, 404→`NotFound`, 400→`ApiError`,
  rede/timeout→`NetworkError(5)`, 403-em-path→`PermissionDenied(7)`.
- **Detectar servidor desligado** (JupyterHub): resposta redireciona p/ `/hub/` ou login →
  `ServerDownError` com hint "abra o JupyterHub e clique Start My Server".

---

## 6. `sync.py` + `index.py` — diff 3-way e planos

**Detecção de mudança (§ research sync-safety §1):**
- Local mudou? quick-check `size`/`mtime_ns` vs index → se difere, confirma por **sha256** vs
  `index.local_sha256`. Regra racy-git: `mtime_ns >= synced_at` força recálculo de sha.
- Remoto mudou? `last_modified`+`size` vs index (sem hash confiável nesta instância). Antes de
  **sobrescrever**, baixa e compara sha256 (definitivo).

**Tabela de decisão por arquivo (núcleo):**

| local vs base | remoto vs base | `push` | `pull` |
|---|---|---|---|
| = | = | nada | nada |
| mudou | = | upload | nada |
| = | mudou | nada | download |
| **mudou** | **mudou** | **CONFLITO→aborta** | **CONFLITO→aborta** |
| novo | ausente | upload(cria) | nada |
| ausente | novo | nada | download(cria) |
| sumiu | = base | reporta (não deleta remoto) | re-download (restaura) |
| = base | sumiu | não recria sozinho¹ | reporta (não deleta local) |

¹ presente-na-base-mas-sumiu-de-um-lado → reporta, pede decisão explícita; nunca delete auto.

**Status codes (saída estilo git):** `A` add, `M` modify, `C` conflict, `?` untracked,
`!` sumiu de um lado, `S` skipped (dotfile/ignore), `D` delete (só com flag explícita).

**push (resumo do algoritmo):** para cada candidato → `remote_path_for`+`assert_within_prefix`
→ re-GET meta (corrida) → se remoto divergiu da base e não `--force`: conflito, pula → se
arquivo remoto existe e **não** está na base: `checkpoint` defensivo → `put_dir` dos pais
(nomes sem ponto) → `put_file` → re-GET verify (size) → **só então** atualiza index. Conflitos
abortam o run por padrão (fail-safe); `--keep-going` processa limpos e falha no fim.

**pull:** simétrico; `safe_local_dest` + `atomic_write`; nunca deleta local; conflito não
sobrescreve (salva `<arquivo>.remote-conflict-<ts>` opcionalmente).

**Limites de massa:** abortar plano > N arquivos ou > X MB sem `--force` (anti-catástrofe por
bug de diff). **check-access:** se o GET do prefixo falhar/vier vazio inesperado → abortar
(nunca interpretar "remoto vazio" como "deletar tudo").

**Index atualizado SÓ após sucesso verificado** (self-healing: falha no meio → re-detecta).
**`--dry-run` e `status` são garantidamente read-only** (nem o index é tocado).

---

## 7. `jp rm` / delete — ultra-gated (separado de push/pull)

- `jp rm --remote <path>`: valida path-jail; recusa wildcard/recursão sem `--recursive`;
  `--max-delete` (default 5) sem `--force`; **dry-run primeiro** listando exatamente o que
  apaga; **confirmação digitada** (usuário digita o nome/quantidade); `checkpoint` antes
  quando possível. **DELETE de dir não-vazio retorna 400 (servidor NÃO recursa)** → `jp rm
  --recursive` lista a subárvore e apaga **bottom-up** (arquivos → subdirs mais profundos →
  dir), cada DELETE validado por path-jail; gate ainda mais forte para diretórios (digitar o
  nome do dir).
- `jp clean` (local): idem dry-run+confirmação; nunca toca arquivos fora do escopo
  rastreado/index.

---

## 8. `ignore.py` — ignores e política de dotfiles

**Embutidos sempre ativos (não removíveis), nos dois sentidos:**
`.git/`, `.jp/`, `__pycache__/`, `*.pyc`, `.DS_Store`, `.ipynb_checkpoints/`, `.stversions/`,
`jp-tmp/`, `*.swp`, `Thumbs.db`. (Arquivos ignorados nunca são deletados.)

**`.jpignore`** (opcional, sintaxe `.gitignore`: globs, `!` negação, `#` comentário, `/`
ancorado) via `fnmatch`.

**Política de dotfiles** (config `dotfiles`, default `skip`) — por causa de `allow_hidden=False`:
- `skip` (default): paths ocultos não entram no plano de push; reportados como `S` no resumo,
  **nunca** geram HTTP 400, **nunca** abortam o run.
- `mangle` (opt-in): sobe `.gitignore` como `dot__gitignore` no remoto, reverte no pull;
  mapeamento no index; avisa trade-offs (quebra UI/interop); **nunca** para segredos.
- `error`: tenta e falha explícito (debug).

**Capability probe** (no clone/doctor): PUT de arquivo oculto temporário em `jp-tmp/` →
201 ⇒ `serverallowshidden=true` (não pula); 400 ⇒ `false` (pula). Grava no config.

⚠️ **Efeito de segurança bom:** `.env`, `*.token`, `.jupyter_ufsc_token` são ocultos →
o skip default já impede subir segredos sem querer.

---

## 9. `cli.py` — comandos (argparse, set_defaults(func))

Flags globais (parent parser, válidas antes e depois do subcomando): `-v/--verbose` (count),
`-q/--quiet`, `--json`, `--color {auto,always,never}`, `--no-color`, `--timeout S`,
`--token-file PATH`, `-C <dir>`.

**Fase 1 (MVP — sync seguro):**
```
jp clone <url> [<dir>] [-n]           # cria <dir>+.jp/, baixa árvore (com sumário+confirmação)
jp init [<dir>] [--server][--prefix]  # inicializa .jp/ numa pasta existente
jp login [--server][--token-file]     # onboarding: token (getpass), testa /api/me, grava global
jp pull [<path>...] [-n][--force][-y]
jp push [<path>...] [-n][--force][-y][--keep-going][--mangle-dotfiles]
jp status [<path>...] [--json][-s]     # read-only
jp ls [<remote>] [-l][-R][--json]      # read-only, não toca disco
jp diff [<path>...] [--remote][--stat]
jp config [get|set|unset|list] [k] [v] [--global]
jp ignore [<pattern>...] [--list]
jp rm --remote <path> [--recursive][--max-delete N][--force]   # gated (§7)
jp doctor [--json]                     # token? conectividade? versão? perms? hidden? relógio?
jp version [--json]
```

**Fase 2 (kernel):** `jp run <file|->`, `jp exec "<code>"`, `jp kernel [list|stop|interrupt]`
via Sessions/Kernels API + mini-WS stdlib (`ws.py`). Isolado; não bloqueia o MVP.

**Exit codes:** 0 ok · 1 genérico · 2 uso (argparse) · 3 não-é-repo · 4 auth · 5 rede ·
6 conflito · 7 permissão · 8 path-unsafe · 130 SIGINT. Via `JpError.exit_code`.

---

## 10. `ui.py` — saída

- Cor: `use_color()` respeita `--color`, `NO_COLOR`, `CLICOLOR_FORCE`, `isatty`, `TERM=dumb`;
  Windows habilita VT via `SetConsoleMode` (sem `colorama`). Verde=ok/add, vermelho=erro/del,
  amarelo=modify/warn, dim=info.
- Progresso em **stderr**, desligado em `--quiet`/`--json`/não-tty.
- **stdout = dados** (consumível por script); **stderr = humano** (progresso, prompts, erros).
- Erros acionáveis: `o quê + por quê + como consertar`. Sem traceback ao usuário (só `-vv`).
- `--json`: só JSON em stdout, zero cor/progresso/prompt.
- `redact()` aplicado a tudo (token → `***`).
- Confirmação destrutiva: prompt por padrão; `-y/--yes` pula; não-tty sem `-y` → **aborta**.

---

## 11. Distribuição (packaging)

- `pyproject.toml`: hatchling, src-layout, `requires-python=">=3.8"`, `dependencies=[]`,
  `[project.scripts] jp="jp.cli:main"`, versão dinâmica de `src/jp/__init__.py`.
- **Canal primário:** PyPI → `uv tool install jp-cli` (ou `pipx install jp-cli`).
- **Releases:** `jp.pyz` (zipapp, leve, precisa de Python) + binários PyInstaller por-OS
  (macos-arm64/x86_64, linux-x86_64, windows-x86_64) + `install.sh`/`install.ps1` +
  `SHA256SUMS`.
- **CI:** ci.yml (matriz OS×Py: ruff+mypy+pytest); release.yml (tag `v*` → build → PyPI
  Trusted Publishing OIDC + GH Release). Nome PyPI a confirmar (`jp-cli` provável; `jp` pode
  estar tomado).
- `jp --version` robusto: `__version__` literal com fallback `importlib.metadata` (zipapp/
  PyInstaller podem não ter dist-info).

---

## 12. Testes — gate de release

- **P0 (antes de qualquer comando destrutivo):** path-jail (os 2 sentidos), no-root,
  no-delete-default, no-overwrite-on-conflict, atomic-write, no-symlink-write-through,
  download-containment, dry-run-no-writes. Servidor mockado (`FakeApi`) — CI não bate no real.
- **P1:** token nunca em output/repo, gitignore presente, perms do token, TLS, post-write
  verify, readonly-shared, config revalidada.
- **P2:** audit log, root-discovery bounded, clone-target-safe, dotfiles skip/resiliência.
- **Teste de integração manual** (não-CI): contra o servidor real, sempre em sandbox
  `privado/jp-tmp-test/` (sem ponto), criado e deletado, `privado` preservado.

---

## 13. Mapa pesquisa → seção

- sync-safety → §1,§6,§7 · threat-model (20 INV) → §4,§5,§10,§12 · ux-cli → §3,§9,§10 ·
  packaging → §2,§11 · repo-structure → §2 · jupyter-api(empírico) → §1,§5 ·
  dotfiles → §1,§8.

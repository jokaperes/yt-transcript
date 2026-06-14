# YouTube Transcripts

> 🇬🇧 English below · 🇧🇷 [Versão em português](#-português)

CLI and GUI that use `yt-dlp` to download YouTube audio and Whisper to produce
transcripts in any language. The default backend is `faster-whisper`, the
recommended path for NVIDIA GPUs.

---

## 🍪 Cookies that never expire (read this first)

YouTube blocks most server/desktop downloads with a bot / "sign in to confirm"
check. **You basically can't download reliably without cookies.** The fix is to
give the app a `cookies.txt`, and the trick is exporting it the *durable* way so
it does **not** expire after the first download.

### The durable method (this is the one that lasts forever)

1. Install a cookie-export browser extension (any "Get cookies.txt" type works;
   this repo ships one under `yt-cookie-extension`).
2. Open an **incognito / private window**.
3. Log into a **throwaway YouTube account — one you never use anywhere else.**
4. Export the cookies and save the file as **`cookies.txt`**, next to the app
   (the same folder as `yt-transcript.exe`, or wherever you run the engine).
5. **Close the incognito window BEFORE you download anything.**
6. From now on always point the app at that file:
   - GUI: put the path in the **Cookies file** field.
   - CLI: add `--cookies cookies.txt`.

### Why it stays alive forever

yt-dlp **rotates** the YouTube session cookies on every download and **writes the
renewed values back** into the `cookies.txt` you gave it. So the file keeps
re-signing itself — it never goes stale, as long as nothing else touches that
session.

That's exactly why the incognito + throwaway-account + close-the-window steps
matter:

- **Incognito + an account you don't use** → no other browser tab is logged into
  that account rotating the session behind your back.
- **Close the window before downloading** → the browser stops owning the session,
  so yt-dlp becomes the only thing rotating it. If you leave the window open (or
  later open that account in a normal browser), the browser rotates the cookies
  on *its* side and **invalidates your exported file** — that's the "it expires
  every time" problem.

The engine also keeps a normalized copy (`normalized-youtube-cookies.txt`) in the
output folder and **reuses the rotated copy** instead of regenerating it from your
original export, so the renewed values are never thrown away. Only re-export when
it genuinely breaks (a fresh export, being newer, supersedes the rotated copy).

> ⚠️ Treat `cookies.txt` like a password. It's account credentials — never commit
> it, never share it. It's already gitignored in this repo.

---

## Windows App (recommended)

Download the latest **`yt-transcript-windows.zip`** from the
[Releases page](https://github.com/jokaperes/yt-transcript/releases), extract it
anywhere, and run **`yt-transcript.exe`**. It's a self-contained build (Tkinter
GUI + bundled `faster-whisper` engine + `yt-dlp`) — no Python install required.
`ffmpeg` is still needed on `PATH` (see [Setup](#setup-from-source)).

The app **updates itself**: *Check for updates* fetches the latest GitHub release
and, when a newer version exists, downloads the zip and applies it in place — so
you only install manually once.

Features:

- **Batch URLs** — paste multiple URLs, one per line; processed sequentially with
  per-video error recovery
- **Language selection** (Portuguese, English, Spanish, French, …)
- **Output format checkboxes** (MD, TXT, SRT, VTT, JSON)
- **Cookies file** field (see the cookie section above)
- **GPU acceleration** with automatic CPU fallback
- **Estimated time remaining (ETA)** in the status bar
- **System tray icon** — minimize to tray while processing
- **Settings persistence** between sessions
- **Live progress** — download logs, transcription progress, queue counter (3/5)

### GPU notes

For NVIDIA GPU acceleration on Windows, just install a current NVIDIA driver. On
the first GPU run the app downloads the CUDA runtime it needs (cuBLAS + cuDNN,
~1.2 GB, one time) into `%LOCALAPPDATA%\yt-transcript\cuda`.

On RTX 50-series (Blackwell) cards the app forces `float16` automatically, because
the `int8` path crashes inside cuBLAS on that architecture. If the GPU can't be
used at all, transcription falls back to CPU (int8) instead of failing.

---

## Setup from source

```bash
cd yt-transcript
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements-windows.txt
```

`ffmpeg` is required. On Windows:

```powershell
winget install Gyan.FFmpeg   # or: choco install ffmpeg
```

## CLI usage

```bash
python transcribe_youtube.py "https://www.youtube.com/watch?v=VIDEO_ID" --device cuda --cookies cookies.txt
```

Common options:

```bash
# language (default: pt)
python transcribe_youtube.py "URL" --language en --device cuda

# output formats (repeatable)
python transcribe_youtube.py "URL" --format md --format txt --format srt --format vtt --format json

# batch: one URL per line, lines starting with # ignored, failures don't stop the batch
python transcribe_youtube.py --urls-file urls.txt --device cuda --cookies cookies.txt
```

### Output formats

All outputs use the pattern `.{lang}.{fmt}`:

- `.pt.md` — Markdown built for feeding an AI (metadata header + timestamped
  paragraphs). Default format.
- `.pt.txt` — plain transcript with timestamps
- `.pt.srt` — SRT subtitles
- `.pt.vtt` — WebVTT subtitles
- `.pt.json` — segments plus source metadata

Audio files (`.mp3`) are deleted after transcription.

## Model choice

For an RTX 5070 (12 GB VRAM):

```bash
python transcribe_youtube.py "URL" --backend faster-whisper --device cuda --compute-type float16 --model large-v3-turbo
```

- `large-v3-turbo` — best default: strong quality, much faster than full `large-v3`.
- `large-v3` — maximum accuracy when you can wait.
- `medium` — smaller download / lower VRAM.
- `small` / `base` — quick drafts only.

## Building the Windows app

Built by GitHub Actions on every `v*` tag
(`.github/workflows/windows-release.yml`):

1. PyInstaller bundles `yt_transcript_gui.py` (GUI + in-process `faster-whisper`)
   into `dist/yt-transcript/` (onedir).
2. Unused ffmpeg/Pillow codecs and large optional modules are stripped; `yt-dlp`
   ships as a bundled module (the app re-invokes its own exe) — the ML native
   libs (CTranslate2, onnxruntime VAD, OpenBLAS, ffmpeg audio) are the size floor.
3. Everything is zipped to `yt-transcript-windows.zip` (~95 MB) and published to
   Releases.

## Building the macOS Apple Silicon app

Built by GitHub Actions on every `v*` tag
(`.github/workflows/macos-release.yml`):

1. PyInstaller bundles `yt_transcript_gui.py` on `macos-14` (arm64) into
   `dist/yt-transcript/` (onedir).
2. The macOS runtime forces CPU/int8 because `faster-whisper` / CTranslate2 does
   not provide a Metal backend.
3. Everything is packaged as `yt-transcript-macos-arm64.tar.gz` and published to
   Releases.

The macOS app is unsigned and not notarized. On first launch, if Gatekeeper says
the app cannot be opened because the developer cannot be verified, run:

```bash
xattr -dr com.apple.quarantine yt-transcript.app
```

To cut a release: bump `__version__` in `yt_transcript_gui.py`, then push a
matching tag, e.g. `git tag v2.5.0 && git push origin v2.5.0`.

---
---

# 🇧🇷 Português

CLI e GUI que usam `yt-dlp` pra baixar o áudio do YouTube e o Whisper pra gerar
transcrições em qualquer idioma. O backend padrão é o `faster-whisper`, o
recomendado pra GPUs NVIDIA.

---

## 🍪 Cookies que nunca expiram (leia isto primeiro)

O YouTube bloqueia a maioria dos downloads de servidor/desktop com uma checagem
de bot / "faça login pra confirmar". **Na prática, sem cookies você não consegue
baixar de forma confiável.** A solução é dar um `cookies.txt` pro app, e o pulo do
gato é exportar do jeito *durável* pra ele **não** expirar depois do primeiro
download.

### O método durável (esse é o que dura pra sempre)

1. Instale uma extensão de exportar cookies (qualquer uma do tipo "Get
   cookies.txt"; este repo tem uma em `yt-cookie-extension`).
2. Abra uma **janela anônima / privada**.
3. Faça login numa **conta do YouTube descartável — uma que você NÃO usa em
   lugar nenhum.**
4. Exporte os cookies e salve o arquivo como **`cookies.txt`**, na mesma pasta do
   app (junto do `yt-transcript.exe`, ou onde você roda o engine).
5. **FECHE a janela anônima ANTES de baixar qualquer coisa.**
6. A partir daí, sempre aponte o app pra esse arquivo:
   - GUI: coloque o caminho no campo **Cookies file**.
   - CLI: adicione `--cookies cookies.txt`.

### Por que ele dura pra sempre

O yt-dlp **rotaciona** os cookies de sessão do YouTube a cada download e
**regrava os valores renovados de volta** no `cookies.txt` que você passou. Ou
seja, o arquivo se reassina sozinho — ele nunca fica velho, desde que mais nada
mexa naquela sessão.

É exatamente por isso que os passos anônima + conta-descartável +
fechar-a-janela importam:

- **Anônima + conta que você não usa** → nenhuma outra aba está logada naquela
  conta rotacionando a sessão pelas suas costas.
- **Fechar a janela antes de baixar** → o navegador para de ser o dono da sessão,
  então o yt-dlp passa a ser a única coisa rotacionando ela. Se você deixar a
  janela aberta (ou depois abrir essa conta num navegador normal), o navegador
  rotaciona os cookies do **lado dele** e **invalida o seu arquivo** — é esse o
  problema do "expira toda vez".

O engine ainda mantém uma cópia normalizada (`normalized-youtube-cookies.txt`) na
pasta de saída e **reusa a cópia rotacionada** em vez de regerar a partir da sua
exportação original, pra nunca jogar fora os valores renovados. Só reexporte
quando realmente quebrar (uma exportação nova, por ser mais recente, substitui a
cópia rotacionada).

> ⚠️ Trate o `cookies.txt` como senha. É credencial de conta — nunca faça commit,
> nunca compartilhe. Já está no gitignore deste repo.

---

## App Windows (recomendado)

Baixe o **`yt-transcript-windows.zip`** mais recente na
[página de Releases](https://github.com/jokaperes/yt-transcript/releases),
extraia em qualquer lugar e rode o **`yt-transcript.exe`**. É um build
self-contained (GUI Tkinter + engine `faster-whisper` + `yt-dlp` embutidos) — não
precisa instalar Python. O `ffmpeg` ainda precisa estar no `PATH` (veja
[Setup](#setup-pelo-código-fonte)).

O app **se atualiza sozinho**: *Check for updates* busca a última release do
GitHub e, quando há versão nova, baixa o zip e aplica no lugar — você só instala
manualmente uma vez.

Recursos:

- **URLs em lote** — cole várias URLs, uma por linha; processadas em sequência com
  recuperação de erro por vídeo
- **Seleção de idioma** (português, inglês, espanhol, francês, …)
- **Checkboxes de formato** (MD, TXT, SRT, VTT, JSON)
- Campo **Cookies file** (veja a seção de cookies acima)
- **Aceleração por GPU** com fallback automático pra CPU
- **Tempo estimado restante (ETA)** na barra de status
- **Ícone na bandeja** — minimiza enquanto processa
- **Persistência das configurações** entre sessões
- **Progresso ao vivo** — logs de download, progresso da transcrição, contador da
  fila (3/5)

### Notas de GPU

Pra acelerar por GPU NVIDIA no Windows, basta instalar um driver NVIDIA atual. Na
primeira run com GPU o app baixa o runtime CUDA que precisa (cuBLAS + cuDNN,
~1,2 GB, uma vez) em `%LOCALAPPDATA%\yt-transcript\cuda`.

Em placas RTX série 50 (Blackwell) o app força `float16` automaticamente, porque o
caminho `int8` dá crash dentro do cuBLAS nessa arquitetura. Se a GPU não puder ser
usada de jeito nenhum, a transcrição cai pra CPU (int8) em vez de falhar.

---

## Setup pelo código-fonte

```bash
cd yt-transcript
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements-windows.txt
```

O `ffmpeg` é obrigatório. No Windows:

```powershell
winget install Gyan.FFmpeg   # ou: choco install ffmpeg
```

## Uso pela CLI

```bash
python transcribe_youtube.py "https://www.youtube.com/watch?v=VIDEO_ID" --device cuda --cookies cookies.txt
```

Opções comuns:

```bash
# idioma (padrão: pt)
python transcribe_youtube.py "URL" --language en --device cuda

# formatos de saída (pode repetir)
python transcribe_youtube.py "URL" --format md --format txt --format srt --format vtt --format json

# lote: uma URL por linha, linhas com # são ignoradas, falhas não param o lote
python transcribe_youtube.py --urls-file urls.txt --device cuda --cookies cookies.txt
```

### Formatos de saída

Todos seguem o padrão `.{idioma}.{fmt}`:

- `.pt.md` — Markdown feito pra alimentar uma IA (cabeçalho de metadados +
  parágrafos com timestamp). Formato padrão.
- `.pt.txt` — transcrição simples com timestamps
- `.pt.srt` — legendas SRT
- `.pt.vtt` — legendas WebVTT
- `.pt.json` — segmentos + metadados da fonte

Os arquivos de áudio (`.mp3`) são apagados após a transcrição.

## Escolha de modelo

Pra uma RTX 5070 (12 GB de VRAM):

```bash
python transcribe_youtube.py "URL" --backend faster-whisper --device cuda --compute-type float16 --model large-v3-turbo
```

- `large-v3-turbo` — melhor padrão: ótima qualidade, bem mais rápido que o
  `large-v3` completo.
- `large-v3` — precisão máxima quando dá pra esperar.
- `medium` — download menor / menos VRAM.
- `small` / `base` — só pra rascunho rápido.

## Buildando o app Windows

Buildado pelo GitHub Actions a cada tag `v*`
(`.github/workflows/windows-release.yml`):

1. O PyInstaller empacota o `yt_transcript_gui.py` (GUI + `faster-whisper` no
   processo) em `dist/yt-transcript/` (onedir).
2. Codecs ffmpeg/Pillow não usados e módulos opcionais grandes são removidos; o
   `yt-dlp` vai como módulo embutido (o app reinvoca o próprio exe) — as libs
   nativas de ML (CTranslate2, onnxruntime VAD, OpenBLAS, ffmpeg) são o piso de
   tamanho.
3. Tudo é zipado em `yt-transcript-windows.zip` (~95 MB) e publicado nas Releases.

## Buildando o app macOS Apple Silicon

Buildado pelo GitHub Actions a cada tag `v*`
(`.github/workflows/macos-release.yml`):

1. O PyInstaller empacota o `yt_transcript_gui.py` no `macos-14` (arm64) em
   `dist/yt-transcript/` (onedir).
2. O runtime do macOS forca CPU/int8 porque `faster-whisper` / CTranslate2 nao
   tem backend Metal.
3. Tudo e empacotado como `yt-transcript-macos-arm64.tar.gz` e publicado nas
   Releases.

O app macOS nao e assinado nem notarizado. No primeiro launch, se o Gatekeeper
disser que o app nao pode ser aberto porque o desenvolvedor nao foi verificado,
rode:

```bash
xattr -dr com.apple.quarantine yt-transcript.app
```

Pra cortar uma release: suba o `__version__` no `yt_transcript_gui.py` e empurre
uma tag correspondente, ex.: `git tag v2.5.0 && git push origin v2.5.0`.

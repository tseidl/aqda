# AQDA — Augmented Qualitative Data Analysis

**A free, open-source tool for qualitative researchers. AI-powered, local-first, privacy-respecting.**

AQDA gives you a modern coding interface with local AI assistance — without cloud subscriptions, without your data ever leaving your machine. It runs as a local web app in your browser.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What Can AQDA Do?

### Core Qualitative Coding

- **Text coding** — Select text, apply codes, build your codebook. Click on any coded passage to see applied codes or remove them.
- **Hierarchical codes** — Organize codes in parent-child trees with colors and descriptions. Drag and drop in the sidebar to re-parent or reorder.
- **Image & audio support** — Import images (JPG, PNG, GIF, WebP) and audio files (MP3, WAV, M4A) with optional local transcription via Whisper.
- **Memos** — Write analytical notes at the project, document, or code level. Anchor a memo to a specific passage and jump back to it, and reference codes or other memos inline by typing `@` — click a reference to jump straight to it.
- **Document variables & tags** — Add metadata (author, date, source) to documents, auto-extracted from filenames on import. Give a document a short tag (e.g. `INT`) shown next to it in the sidebar.
- **Coder identity** — Set your name in Settings; each coding records who made it, so collaborators show up as distinct coders in REFI-QDA exports.
- **Segments browser** — Browse all coded segments across documents. Click to jump to the passage in context, or delete directly from the list.
- **Export** — REFI-QDA (.qdpx) for MAXQDA/ATLAS.ti/NVivo, codebook (.qdc), CSV, JSON.

### AI-Powered Augmentation

AQDA uses [Ollama](https://ollama.com) to run AI models locally on your computer. No internet connection required, no data shared with anyone.

| Feature | What it does |
|---------|-------------|
| **Topic Search** | Find passages across your documents that match a topic or theme you describe |
| **Code Suggest** | Given a code, find uncoded passages that might belong to it (from its definition and coded examples); review each and **Apply** or **Dismiss** it |
| **Consistency Check** | Flag coded segments that seem like outliers within a code — like inter-rater reliability with yourself over time |
| **Hierarchy Suggest** | After inductive coding, get suggestions for grouping your codes into parent categories |
| **Code Definition Generator** | Applied a code many times but haven't written a definition yet? Generate one from the actual coded passages |

When you click on an AI result, AQDA jumps to the passage in the document and highlights it, so you can immediately see the context and decide whether to code it.

Topic Search and Code Suggest cover text, PDF, and transcribed audio. Mark any document as **Reference** from its header (e.g. pre-coded examples or training material) to keep it out of AI results.

These tools are designed as a **methodological interlocutor** — they interrogate your coding rather than generate it. The researcher always has the final word.

### Two Types of AI Models

AQDA uses two types of models for different purposes:

| Model type | What it does | Used by | Recommended model |
|-----------|-------------|---------|-------------------|
| **Embedding model** | Converts text into numerical representations so similar passages can be found | Topic Search, Code Suggest, Consistency Check | `nomic-embed-text` (fast, 274 MB) |
| **LLM (language model)** | Reads text and generates structured output (definitions, groupings) | Hierarchy Suggest, Define Code, Text Analysis | `qwen3.5:9b` (6 GB) |

You need one of each. They are configured in **Settings**.

---

## Getting Started

### What You Need

- **Python 3.10 or newer**
- **pipx** (installs Python apps in isolated environments)
- **Chrome, Firefox, or Brave** — Safari has known issues with large file imports and downloads
- **Ollama** (optional, for AI features) — [ollama.com/download](https://ollama.com/download)

### Install

Open a terminal and run:

```bash
pipx install git+https://github.com/tseidl/aqda.git
```

Then start AQDA:

```bash
aqda
```

This opens your browser at `http://127.0.0.1:8765`. To stop, press `Ctrl+C` in the terminal.


<details>
<summary><strong>Don't have Python or pipx?</strong></summary>

**Mac:**
```bash
# Install Homebrew (skip if you already have it)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
After Homebrew installs, it prints commands to add it to your PATH — copy and run those lines, then:
```bash
brew install python pipx
pipx ensurepath
```
Close and reopen Terminal, then install AQDA.

**Windows:**

Download Python from [python.org](https://www.python.org/downloads/) — **check "Add python.exe to PATH"** during installation. Then:
```bash
pip install pipx
pipx ensurepath
```
Close and reopen Command Prompt, then install AQDA.
</details>

<details>
<summary><strong>Getting "command not found" after install?</strong></summary>

Run `pipx ensurepath`, then **close and reopen your terminal**. This adds pipx's install directory to your PATH.
</details>

### Setting Up AI Features (Optional)

1. [Download and install Ollama](https://ollama.com/download)
2. Open a terminal and pull the models:
   ```bash
   ollama pull nomic-embed-text   # for similarity search
   ollama pull qwen3.5:9b         # for analysis and definitions
   ```
3. In AQDA, go to **Settings** and select your models under "Embedding Model" and "LLM Model"
4. Open the **AI panel** (sparkle icon in the left sidebar)

All AI processing happens on your machine. Nothing is sent to any server.

### Audio Transcription (Optional)

To transcribe audio files locally using Whisper:

```bash
pipx inject aqda "aqda[audio]"
```

Then import an audio file (MP3, WAV, M4A) and click the transcribe button.

### Auto-Extract Metadata from Filenames (Optional)

If your files follow a naming convention, AQDA can automatically extract variables on import. In **Settings → Filename Variable Parsing**, set a regex pattern with named groups.

For example, files like `2025-03-10_guardian_from-border-crackdown.txt`:

```
(?P<date>\d{4}-\d{2}-\d{2})_(?P<source>[^_]+)_(?P<title>.+)
```

This extracts `date`, `source`, and `title` as document variables automatically when you import.

### Updating

```bash
pipx install --force git+https://github.com/tseidl/aqda.git
```

### Uninstalling

```bash
pipx uninstall aqda
```

This removes the app but keeps your data in `~/.aqda/`. To remove everything, also delete that folder.

---

## Your Data

AQDA saves every change immediately. There is no Save button. Its private working database
lives at `~/.aqda/aqda.db`; normal users never need to open or move this file.

- **Automatic backups** — AQDA keeps seven verified daily backups in `~/.aqda/backups/`
  and creates an extra backup before migrations or replacing a project from a collaborator
- **Move or archive a project** with an `.aqda` snapshot from the Export menu
- **Deleted projects** go to a trash bin and can be restored

Do not put the live `aqda.db` in Google Drive, Dropbox, OneDrive, or a network folder.
AQDA's collaboration feature below provides the same convenient shared-folder experience
without exposing a live SQLite database to cloud-sync races.

To restore a full backup, close AQDA, keep the current `aqda.db` as an extra copy, and copy
the chosen backup into its place as `aqda.db`.

### Collaboration — Google Drive, Dropbox, or a Shared Folder

Collaboration is designed to feel like opening the same document from a shared folder.
AQDA quietly uses a safe local working copy and syncs complete, closed snapshots in the
background. You never manage the local copy and you never need to save manually.

**Set it up once:**

1. Open **Settings → Collaboration**
2. Choose a folder inside Google Drive, Dropbox, OneDrive, or another synced location
3. Open a project and click **Share project**
4. On the other researcher's computer, choose the same collaboration folder in Settings
5. The project appears under **Shared projects available**; click **Open project** once

After that, both researchers open the project normally from AQDA's project list. Changes
save locally immediately and complete snapshots are published to the shared folder after a
short delay. Incoming changes appear automatically. Before replacing local project data,
AQDA creates and verifies a full safety backup.

If two people happen to work at the same time—or one computer was offline—AQDA detects the
two histories and keeps both as clearly named projects. Neither person's work is overwritten.
You can compare them and decide which version to continue using.

Under the hood, the collaboration folder contains an `.aqda-project` folder with immutable
snapshots. These are managed by AQDA; collaborators should not rename or edit them manually.

**Stopping AQDA:** use the **Close AQDA** button, or press Ctrl+C once in the terminal. Both
perform a graceful final sync. Closing only the browser tab leaves the local AQDA server
running, which is harmless; reopen `http://127.0.0.1:8765` to return. If the computer stops
unexpectedly, the hidden local copy is retained and syncs on the next launch.

> **Not supported:** automatically merging two independently edited versions into one.

For one-person local work, nothing changes: create a project, work normally, and close AQDA.
Everything autosaves; no files or caches need to be managed.

---

## Export Formats

| Format | Use case |
|--------|----------|
| `.aqda` | Share a project with another AQDA user — full round-trip import/export |
| `.qdpx` | REFI-QDA text exchange — import into MAXQDA, ATLAS.ti, NVivo |
| `.qdc` | Codebook XML — share code hierarchies between projects |
| `.csv` | Coded segments as a table — for further analysis in R, Excel, etc. |
| `.json` | Analysis data and document variables — for R, Python, or custom processing |

QDPX currently exports text and audio transcripts as text sources. Original audio and image
media are not embedded in the QDPX package; use `.aqda` when an exact AQDA round-trip is needed.

---

## License

MIT

## Acknowledgments

Built with substantial assistance from [Claude Code](https://claude.ai/code) (Claude Opus 4.6 by [Anthropic](https://anthropic.com)). Architecture, backend, frontend, and AI integration were developed collaboratively through human-AI pair programming.

Inspired by [QualCoder](https://github.com/ccbogel/QualCoder) and the qualitative research community's need for modern, accessible, AI-augmented analysis tools.

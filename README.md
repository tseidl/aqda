# AQDA — Augmented Qualitative Data Analysis

**A free, open-source tool for qualitative researchers. AI-powered, local-first, privacy-respecting.**

AQDA gives you a modern coding interface with local AI assistance — without cloud subscriptions, without your data ever leaving your machine. It runs as a local web app in your browser.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What Can AQDA Do?

### Core Qualitative Coding

- **Text coding** — Select text, apply codes, build your codebook. Click on any coded passage to see applied codes or remove them.
- **Hierarchical codes** — Organize codes in parent-child trees with colors and descriptions.
- **Image & audio support** — Import images (JPG, PNG, GIF, WebP) and audio files (MP3, WAV, M4A) with optional local transcription via Whisper.
- **Memos** — Write analytical notes at the project, document, or code level.
- **Document variables** — Add metadata (author, date, source) to documents. Auto-extract from filenames on import.
- **Segments browser** — Browse all coded segments across documents. Click to jump to the passage in context, or delete directly from the list.
- **Export** — REFI-QDA (.qdpx) for MAXQDA/ATLAS.ti/NVivo, codebook (.qdc), CSV, JSON.

### AI-Powered Augmentation

AQDA uses [Ollama](https://ollama.com) to run AI models locally on your computer. No internet connection required, no data shared with anyone.

| Feature | What it does |
|---------|-------------|
| **Topic Search** | Find passages across your documents that match a topic or theme you describe |
| **Code Suggest** | Given a code, find uncoded passages that might belong to it — based on its definition and existing coded examples |
| **Consistency Check** | Flag coded segments that seem like outliers within a code — like inter-rater reliability with yourself over time |
| **Hierarchy Suggest** | After inductive coding, get suggestions for grouping your codes into parent categories |
| **Code Definition Generator** | Applied a code many times but haven't written a definition yet? Generate one from the actual coded passages |

When you click on an AI result, AQDA jumps to the passage in the document and highlights it, so you can immediately see the context and decide whether to code it.

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

All your data lives in a single file: `~/.aqda/aqda.db`. This file contains all your projects. You can change the location in **Settings**.

- **Back it up** by copying this file
- **Move between machines** by copying it to another computer
- **Deleted projects** go to a trash bin and can be restored

### Sharing a Project

AQDA supports sharing individual projects via `.aqda` files — small, self-contained databases with everything in that project (documents, codes, codings, memos).

**The workflow:**

1. In your project, click **Export → Share Project (.aqda)** — downloads a file like `My_Project.aqda`
2. Send the file to your collaborator (email, Google Drive, USB stick — whatever works)
3. They open AQDA, click **Import DB** on the project list, and select the `.aqda` file
4. The project appears in their AQDA with all data intact
5. When they're done, they export and send it back

Each import creates a **new project** — it never overwrites existing work. This is turn-based collaboration: one person works at a time.

> **Not supported:** merging two people's independent changes made in parallel.

---

## Export Formats

| Format | Use case |
|--------|----------|
| `.aqda` | Share a project with another AQDA user — full round-trip import/export |
| `.qdpx` | REFI-QDA standard — import into MAXQDA, ATLAS.ti, NVivo |
| `.qdc` | Codebook XML — share code hierarchies between projects |
| `.csv` | Coded segments as a table — for further analysis in R, Excel, etc. |
| `.json` | Full project data — for custom processing or archival |

---

## License

MIT

## Acknowledgments

Built with substantial assistance from [Claude Code](https://claude.ai/code) (Claude Opus 4.6 by [Anthropic](https://anthropic.com)). Architecture, backend, frontend, and AI integration were developed collaboratively through human-AI pair programming.

Inspired by [QualCoder](https://github.com/ccbogel/QualCoder) and the qualitative research community's need for modern, accessible, AI-augmented analysis tools.

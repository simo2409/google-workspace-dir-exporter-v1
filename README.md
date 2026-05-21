# gdrive-dir-exporter

Download all files from a Google Drive folder recursively, recreating the folder structure locally.

## Setup

```bash
uv sync
cp config.default.json config.json
# Edit config.json: set "output" path and "folders" URLs
```

On first run a browser window will open for Google OAuth authorization.
Credentials are shared with other llmwiki utils from:
`~/.config/llmwiki/obs-llmwiki-simone-personal-v1/credentials.json`

## Usage

```bash
uv run main.py
```

## config.json

```json
{
  "output": "~/Downloads/drive-exports/",
  "folders": [
    "https://drive.google.com/drive/folders/YOUR_FOLDER_ID"
  ]
}
```

## Output structure

```
<output>/
├── _metadata.json          ← persists across runs for change detection
└── 2026-05-20/
    └── FolderName/
        ├── subfolder/
        │   └── file.pdf
        └── budget.xlsx
```

## Change detection

Files are skipped on re-runs if unchanged:
- Google-native files (Docs/Sheets/Slides): compared by `modifiedTime`
- Binary files (PDF, images, …): compared by MD5 checksum

Supported export formats: `.docx`, `.xlsx`, `.pptx`, `.svg`

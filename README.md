# Moodboard

Paste links or drop files, and see every image, loop and film on one wall. Each board pulls a color palette from its own images.

**Live:** https://agrawalmukund1506-create.github.io/moodboard/

## Two ways to run it

| | Live site (GitHub Pages) | Local version |
|---|---|---|
| Image and video links, YouTube, Vimeo, Drive, Dropbox | ✓ | ✓ |
| Google Sheets shared as "Anyone with the link" | ✓ | ✓ |
| CSV files | ✓ | ✓ |
| Dropped image and video files | ✓ saved in your browser | ✓ saved to `media/` |
| Pinterest pins and boards | ✓ | ✓ |
| Dribbble, Behance and other page links | Drag the image in instead | ✓ finds the image |
| Excel (.xlsx) files | Export as CSV instead | ✓ |
| Palette colors | From hosts that allow it (Wikimedia, YouTube, Vimeo…) | From every image |
| Where boards are saved | This browser only | `boards/` as JSON files |

### Run the local version
```bash
python3 server.py
```
Then open http://localhost:8765. Needs only Python 3, nothing to install.

## Using it
- Paste links into the bar (many at once is fine), or paste anywhere on the page.
- Drop image or video files, CSV files, or an image dragged from another tab.
- In a sheet or CSV, each row's other cells become the note. A column named **Tag** or **Section** becomes a filter.
- Drag tiles to reorder. Click one for the large view, where you can add a note and copy its colors.
- Click any swatch to copy its hex code.

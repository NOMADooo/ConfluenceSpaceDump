# Confluence Space Dump

This Python script downloads an entire Confluence space and saves it as a static HTML website. It's great for offline backups, archiving, or having a portable copy of your space. The script keeps the page structure, attachments, and images, and tries to make the HTML look like Confluence.

**Key Feature:** You **don't need admin rights** on Confluence to use this script, unlike Confluence's built-in HTML export. If you can read the space, you can export it.

## Features

*   **Export Without Admin:** Downloads all current pages using your regular Confluence access.
*   **HTML Output:** Saves each page as an HTML file.
*   **Attachments & Images:** Downloads all attachments and embedded images, updating links to work locally.
*   **Easy Navigation:**
    *   Creates an `index.html` with a clickable tree of all pages.
    *   Adds breadcrumbs to each page.
*   **Confluence-like Styling:** Uses custom CSS to make pages look familiar. Handles common Confluence elements like info panels, code blocks, and layouts.
*   **Cookie Authentication:** Uses your browser's cookies to log in.
*   **Faster Downloads:** Uses multiple threads to download pages and attachments quicker.
*   **Resume Downloads:** Can skip already downloaded HTML files if a previous download was interrupted.
*   **Clean Filenames:** Creates user-friendly filenames and updates internal links.

## Requirements

*   Python 3.7+
*   pip (Python package installer)

## Setup

1.  **Get the Script:**
    *   Clone with git:
        ```bash
        git clone https://github.com/NOMADooo/ConfluenceSpaceDump
        cd ConfluenceSpaceDump
        ```
    *   Or, download `confluence_space_dump.py`.

2.  **Install Packages:**
    Create a `requirements.txt` file with:
    ```
    requests
    beautifulsoup4
    atlassian-python-api
    tqdm
    python-dateutil
    ```
    Then run:
    ```bash
    pip install -r requirements.txt
    ```
    
## Authentication: Getting Your Cookies

The script needs your browser cookies to access Confluence.

**Why Cookies?**
Many Confluence setups use login systems (like SSO) that are hard for scripts to use directly. Cookies let the script use your existing browser login.

**How to Get Cookies:**

1.  **Log in to Confluence** in your browser (Chrome, Firefox, etc.).
2.  **Open Developer Tools** (usually F12 or right-click -> Inspect).

3.  **Option A: Cookie String (`--cookies` argument)**
    *   Go to the "Network" tab.
    *   Refresh or navigate to a Confluence page.
    *   Find a request to your Confluence domain.
    *   In "Request Headers", find the `Cookie` header. Copy its entire value.
    *   Example: `"JSESSIONID=ABCDEF; another_cookie=XYZ"`

4.  **Option B: Cookie File (`--cookies-file` argument - Recommended)**
    *   Use a browser extension like **"EditThisCookie"** (Chrome/Opera):
        1.  On a Confluence page, click the extension icon.
        2.  Click "Export" (arrow icon) and choose JSON format.
        3.  Save this as `cookies.json` (or any name).
    *   **JSON File Format:**
        ```json
        [
          {
            "name": "JSESSIONID",
            "value": "YOUR_SESSION_ID",
            "domain": ".your-company.atlassian.net", // Important: Match your Confluence domain
            "path": "/"
          },
          {
            "name": "another_cookie",
            "value": "its_value",
            "domain": ".your-company.atlassian.net",
            "path": "/"
          }
        ]
        ```
        Make sure the `domain` in the file matches your Confluence URL.

## How to Use

Run from your terminal:
```bash
python confluence_space_dump.py --space-url <CONFLUENCE_SPACE_URL> [AUTHENTICATION_OPTION] [OTHER_OPTIONS]
```

**Required:**

*   `--space-url <URL>`: URL of your Confluence space (e.g., `https://your.confluence.com/wiki/spaces/SPACEKEY`).
*   **And one of these:**
    *   `--cookies-file <PATH_TO_JSON_FILE>`: Path to your cookies JSON file.
    *   `--cookies "<COOKIE_STRING>"`: Your cookie string (in quotes).

**Optional:**

*   `--output <DIRECTORY>`: Where to save files (default: `./confluence_output`).
*   `--max-workers <NUMBER>`: How many threads for downloads (default: `5`). More can be faster but might overload the server.
*   `--skip-existing`: Skips pages already downloaded (good for resuming).

**Examples:**

*   Using a cookies file:
    ```bash
    python confluence_space_dump.py --space-url https://my.confluence.com/wiki/spaces/PROJ --cookies-file ./my_cookies.json --output ./project_backup
    ```
*   Using a cookie string:
    ```bash
    python confluence_space_dump.py --space-url https://docs.example.com/wiki/spaces/DOCS --cookies "JSESSIONID=ABC; token=XYZ" --skip-existing
    ```

## What You Get

The script creates a folder (e.g., `confluence_output/`) like this:

```
<output_directory>/
├── index.html              # Main navigation page
├── styles/site.css         # Stylesheet
├── images/icons/           # Icons used in the HTML
├── attachments/<page_id>/  # Attachments for each page
│   └── file.pdf
├── Page-Title_pageid.html  # HTML file for a page
└── ...
```
*   Pages are named `Page-Title_pageid.html`.
*   Attachments are in `attachments/<page_id>/filename`.
*   `index.html` lists all pages in a tree.

## Next Steps

The HTML files are great for offline viewing. You can also use them as input for other tools to convert your Confluence content to formats like Markdown, especially for platforms like Obsidian or MkDocs.
Tools like `confluence-to-markdown` or `confluence-to-obsidian` can help with this.

## How It Works (Briefly)

1.  **Setup:** Gets your Confluence URL and cookies. Creates output folders and a basic CSS file.
2.  **Get Page List:** Fetches all current pages in the space using the Confluence API.
3.  **Process Pages:** Downloads each page's HTML and attachments (using multiple threads).
    *   Cleans up the HTML.
    *   Fixes internal links to point to local files.
    *   Updates image paths to local images.
    *   Simplifies Confluence macros for consistent styling.
    *   Adds a list of attachments to each page.
    *   Saves the processed page as an HTML file.
4.  **Create Index:** Builds `index.html` with a clickable tree of all pages.

## Notes & Limitations

*   **Cookies Expire:** If your Confluence login expires, you'll need new cookies.
*   **Dynamic Content:** Complex JavaScript-driven content or embedded external services might not work perfectly.
*   **API Changes:** Future Confluence API updates could break the script.
*   **Rate Limits:** Very large spaces or too many workers might hit Confluence's API limits.
*   **Styling:** The CSS provides a good general look but might not perfectly match your specific Confluence theme.
*   **Errors:** Check the console for errors if some pages don't export correctly.
*   **No JavaScript Rendering:** Content that *requires* JavaScript to appear in the browser won't be captured.
*   **Confluence Space Base Path:** Should always be the same ex. `/wiki/spaces`. See the [official info](https://community.atlassian.com/forums/Confluence-questions/Confluence-path/qaq-p/1173030)

## Development Note

This script was developed with significant assistance from Google's Gemini 2.5 Pro Preview O3-25 especially utilising the 1'048'576 context window.

## Contributing

Found a bug or have an idea? Feel free to open an issue or pull request!

## License

MIT License. See the `LICENSE` file.

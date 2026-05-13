# WebScanner2: Autonomous UI & Security Testing Framework

[![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Playwright](https://img.shields.io/badge/powered%20by-Playwright-green.svg)](https://playwright.dev/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](#license)

**WebScanner2** is a professional-grade, autonomous end-to-end web automation and security testing framework. Designed for senior QA engineers and security researchers, it performs exhaustive, non-repetitive testing of complex web applications by combining BFS crawling, structural DOM analysis, and non-destructive vulnerability signal detection.

**Author:** Avnish Ranjan (<avnishranjan7@gmail.com>)

---

## 🚀 Project Overview

WebScanner2 exists to eliminate the manual effort of mapping and smoke-testing large-scale web applications. Unlike traditional testing tools that require hardcoded selectors, WebScanner2 autonomously discovers interactive elements, identifies state-changing components, and validates functional paths across your entire application.

### Key Capabilities
- **Autonomous Discovery**: Recursively maps pages, routes, and UI components without manual scripting.
- **Exhaustive Testing**: Interacts with buttons, links, inputs, and complex components exactly once per crawl.
- **Security Guardrails**: Passive and safe active scanning for common vulnerability signals.
- **Deduplication Engine**: Global state tracking ensures high efficiency and zero redundant testing.

### Supported Application Types
- **Modern SPAs**: React, Vue, Angular, Svelte.
- **Frameworks**: Next.js, Nuxt.js, Remix.
- **Server-Rendered**: Django, Rails, Laravel, PHP, ASP.NET.
- **Complex UI**: Applications with Shadow DOM, iframes, and dynamic modals.

---

## ✨ Features

- **Autonomous BFS Crawling**: Systematic Breadth-First Search traversal with intelligent route normalization.
- **Dynamic DOM Rescanning**: Recovers from page drift and dynamic updates to ensure stable interactions.
- **Shadow DOM & Iframe Support**: Transparently traverses encapsulated DOM structures and same-origin frames.
- **Intelligent Form Testing**: Grouped testing for checkboxes and radios; dependency-aware dropdown sequence testing.
- **Safe Security Scanning**: Detects exposed secrets, insecure headers, CSRF absence, and potential IDOR patterns.
- **State Deduplication**: Uses structural fingerprints to ensure every element and UI state is tested only once.
- **Recursive UI Testing**: Handles nested modals, accordions, and sliding drawers without infinite loops.
- **Professional Reporting**: Generates detailed HTML summaries, comprehensive Excel manifests, and JSON UI inventories.
- **Interactive Login Handling**: Supports manual login detection and wait-states for authenticated session testing.
- **Network & Console Monitoring**: Actively logs runtime errors, HTTP failures, and sensitive data leaks in console output.

---

## 🏗 Architecture

WebScanner2 is built on a modular, decoupled architecture where specialized engines coordinate through a central crawler brain.

### Module Breakdown
- **Crawler**: The orchestration engine managing the BFS queue, route normalization, and module lifecycle.
- **Interactor**: Executes low-level browser actions (clicks, typing, hovering) with robust retry and recovery logic.
- **Element Finder**: Performs structural DOM inspection to identify interactive elements, including Shadow DOM and sidebar detection.
- **Form Tester**: Specialized logic for complex input groups, dependent dropdowns, and dummy data injection.
- **Security Scanner**: Passive and active signal detection engine for safe vulnerability discovery.
- **State Tracker**: The source of truth for global deduplication, tracking tested elements and visited states.
- **Reporter Suite**: Unified logging system generating HTML, Excel, and JSON outputs.

### Folder Structure
```text
.
├── core/                # Core orchestration and interaction engines
│   ├── browser.py       # Browser lifecycle management
│   ├── crawler.py       # Main BFS traversal logic
│   ├── element_finder.py# DOM analysis and discovery
│   ├── form_tester.py   # Specialized form & input testing
│   ├── interactor.py    # Low-level interaction execution
│   ├── security_scanner.py # Vulnerability signal detection
│   └── state_tracker.py # Global deduplication engine
├── reporting/           # Report generation and logging
│   ├── report_builder.py# HTML/JSON report constructor
│   ├── excel_reporter.py# Spreadsheet generation
│   ├── ui_inventory.py  # Structural UI inventory logic
│   └── console.py       # CLI output formatting
├── utils/               # Helper functions and shared utilities
├── config.py            # Centralized configuration and environment
├── main.py              # Entry point script
└── requirements.txt     # Project dependencies
```

---

## 🛠 Tech Stack

- **Python 3.9+**: Core logic and orchestration.
- **Playwright**: Modern browser automation and network interception.
- **OpenPyXL**: High-fidelity Excel report generation.
- **Pytest-Asyncio**: Asynchronous test infrastructure.
- **Jinja2**: Dynamic HTML report templating.

---

## ⚙️ Installation

### 1. Clone the Repository
```bash
git clone https://github.com/your-repo/web-auto-tester.git
cd web-auto-tester
```

### 2. Set Up Environment
```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
# or: venv\Scripts\activate  # Windows
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

---

## 📖 Usage

### Basic Run
Execute a full autonomous crawl of a target website:
```bash
python main.py --url https://example.com
```

### Headless Mode & Constraints
Run without a visible browser and limit the crawl depth:
```bash
python main.py --url https://app.example.com --headless --max-pages 50
```

### Custom Timeouts
Adjust the global interaction and navigation timeout:
```bash
python main.py --url https://example.com --timeout 20
```

---

## 🔧 Configuration

All settings are managed via `config.py` and can be overridden through environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `HEADLESS` | `false` | Run browser in background mode. |
| `MAX_PAGES` | `100` | Maximum unique routes to visit. |
| `GLOBAL_ELEMENT_DEDUP` | `true` | Ensure elements are tested only once across the whole site. |
| `SECURITY_SCAN_ENABLED` | `true` | Enable passive/active security signal detection. |
| `ACTION_TIMEOUT` | `10000` | Timeout (ms) for individual element interactions. |
| `MAX_MODAL_DEPTH` | `3` | Maximum nesting depth for recursive modal testing. |
| `SHADOW_DOM_ENABLED` | `true` | Enable discovery of elements inside Shadow Roots. |

---

## 📊 Reporting

WebScanner2 provides three levels of reporting to satisfy different stakeholder needs:

1.  **HTML Executive Summary**: A visually rich report with screenshot evidence, success/failure metrics, and security findings.
2.  **Excel Technical Manifest**: A granular spreadsheet containing every interaction, selector, and error message for deep analysis.
3.  **JSON UI Inventory**: A machine-readable structural map of the application, grouped by functional classification.
4.  **Screenshots**: Automatic capture of the page state for every detected failure or security finding.

---

## 🔒 Security Testing

WebScanner2 includes a built-in security layer that scans for high-confidence vulnerability indicators during the crawl.

**Key Checks:**
- **Sensitive Data**: Matching for AWS keys, JWTs, Google API keys, and private keys in responses.
- **Information Disclosure**: Detection of stack traces, SQL errors, and debug pages.
- **Header Security**: Validation of CSP, HSTS, X-Frame-Options, and Secure/HttpOnly cookie flags.
- **Access Controls**: Passive detection of IDOR-like patterns and missing CSRF tokens.
- **Frontend Vulnerabilities**: Monitoring for DOM XSS indicators and insecure client storage (localStorage).

> [!IMPORTANT]
> **Safety Warning**: This tool performs **safe, non-destructive testing only**. It does not attempt to exploit vulnerabilities, bypass authentication, or perform destructive actions (like deleting data).

---

## 🖥 Terminal Output

WebScanner2 features a professional logging system designed for real-time progress monitoring.

- **Banner**: Displays run metadata, target URL, and configuration.
- **Progress Tracking**: Real-time updates as the crawler moves through BFS levels.
- **Interaction Logs**: Colour-coded pass/fail indicators for every button click, form submission, and link navigation.
- **Security Alerts**: Instant highlighting of critical security findings.
- **Final Summary**: Comprehensive count of tested pages, elements, successes, failures, and paths to all generated reports.

---

## 🛡 Performance & Safety

- **Visited State Tracking**: Strict URL normalization prevents infinite loops and redundant page visits.
- **Fingerprint-Based Deduplication**: Every UI element is uniquely identified; even if a page is reloaded, the crawler knows what it has already tested.
- **Graceful Failure Handling**: Elements that fail to respond or time out are logged with screenshots, allowing the crawl to continue without crashing.
- **Safe Retries**: Automatic recovery from page drift and navigation failures.

---

## 📝 Example Output

### Terminal Summary
```text
============================================================================
                              FINAL SUMMARY
============================================================================
  Pages Tested     : 42
  Total Elements   : 847
  Passed Actions   : 812
  Failed Actions   : 35
  Screenshots      : 35
  HTML Report      : /path/to/output/reports/20260508_111402.html
  Excel Report     : /path/to/output/reports/20260508_111402.xlsx
  Security Issues  : 12
  Screenshots Dir  : /path/to/output/screenshots/20260508_111402
============================================================================
  35 FAILURE(S) DETECTED ✗
============================================================================
```

---

## ⚠️ Limitations

- **Business Logic**: The tool cannot validate complex business logic correctness (e.g., verifying if a bank transfer amount was correct).
- **Destructive Actions**: By design, it avoids actions that could delete data or permanently alter the application state.
- **Visibility**: Elements hidden behind complex custom interactions or canvas-based UIs may have limited visibility.

---

## 🤝 Contributing

Contributions are welcome! Please ensure you follow our professional standards:
1. Fork the repository.
2. Create a feature branch (`git checkout -b feature/AmazingFeature`).
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`).
4. Push to the branch (`git push origin feature/AmazingFeature`).
5. Open a Pull Request.

---

## ⚖️ License

Distributed under the MIT License. See `LICENSE` for more information.

---
*Professional enterprise README.md generated successfully.*

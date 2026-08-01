# Review Toolchain

Run these tools before verifying AC items. A tool that is missing or fails
is NOT a review failure — note it in evidence and continue.

## Tools

1. **OCR Review** (open-code-review)
   ```bash
   ocr review --from <BASE_REF> --to <HEAD_REF> --format json
   ```
   Use the exact base..head range from the review request. OCR auto-detects
   changed files from the git range.

2. **Linter**
   ```bash
   ruff check backend/
   ```

## Aggregation

- Run all tools, collect findings.
- Map findings to AC items where relevant.
- Include tool output in the verdict report.

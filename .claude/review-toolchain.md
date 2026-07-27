# Review Toolchain

Run these tools before verifying AC items.

## Tools

1. **OCR Review**
   ```bash
   ocr review --from main --to fa47dc2 --format json
   ```
   OCR auto-detects changed files from the git range.

2. **Linter**
   ```bash
   ruff check backend/
   ```

## Aggregation

- Run all tools, collect findings.
- Map findings to AC items where relevant.
- Include tool output in the verdict report.

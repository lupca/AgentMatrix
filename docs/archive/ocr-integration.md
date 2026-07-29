# OCR Integration Design for LangGraph Gates

> CTV2-068 Design Output

## 1. V1 Reference Summary

### V1 OCR Usage (from `tool-registry.md`)

| Field | Value |
|-------|-------|
| **id** | `ocr` |
| **scope** | `target-repo` |
| **health_check** | `cd <repo_root> && ocr --version` |
| **install** | `npm install -g @alibaba-group/ocr-linux-x64` (Linux) |
| **used_by** | `pm (step 8.5), review (toolchain)` |
| **required** | `soft` for PM pre-scan; `hard` when declared in review-toolchain |

### V1 Preflight Algorithm (from `AGENTS-REFERENCE.md` §8)

```
1. health_check(ocr, scope=target-repo)
2. IF fails:
   a. Run install command
   b. Re-run health_check
3. IF still fails:
   - soft: LOG + skip
   - hard: BLOCK + escalate
4. NEVER skip silently
```

### V1 Pre-scan Flow (from `task-creation.md` step 8.5)

```
1. Preflight: look up ocr in registry, run health check
2. If unavailable → log explicitly, continue (soft)
3. If available → run `ocr scan --path <files> --format json`
4. If findings → record in task body under `## Pre-scan findings (OCR)`
```

## 2. V2 Mapping

| V1 Concept | V2 Gate | Required | Purpose |
|------------|---------|----------|---------|
| PM pre-scan (step 8.5) | `spec_gate` | soft | Surface bugs early → include fixes in Plan |
| Review toolchain | `review_order_gate` | hard (when declared) | Attach findings to Review Sheet |

### Key Differences

| Aspect | V1 | V2 |
|--------|----|----|
| Execution | Spawns CLI via Bash | Calls Python async service |
| State | Stateless CLI | Service with connection pooling |
| Error handling | Exit codes + stderr | Structured exceptions |
| Output | JSON stdout | Pydantic models |

## 3. API Design

### OcrService Class

```python
from pathlib import Path
from pydantic import BaseModel
import asyncio
import shutil

class OcrFinding(BaseModel):
    file: str
    line: int
    rule_id: str
    message: str
    severity: str  # "error" | "warning" | "info"

class OcrScanResult(BaseModel):
    success: bool
    findings: list[OcrFinding]
    skipped_reason: str | None = None

class OcrService:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._binary: Path | None = None
    
    async def health_check(self) -> bool:
        """Check if OCR binary is available and functional."""
        binary = shutil.which("ocr")
        if not binary:
            return False
        
        proc = await asyncio.create_subprocess_exec(
            binary, "--version",
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        if proc.returncode == 0:
            self._binary = Path(binary)
            return True
        return False
    
    async def install(self) -> bool:
        """Attempt to install OCR binary."""
        # Try npm global install
        proc = await asyncio.create_subprocess_exec(
            "npm", "install", "-g", "@alibaba-group/ocr-linux-x64",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return await self.health_check()
    
    async def scan(
        self, 
        files: list[str], 
        required: str = "soft"
    ) -> OcrScanResult:
        """
        Run OCR scan on specified files.
        
        Args:
            files: List of file paths relative to repo_root
            required: "soft" = skip with log on failure, "hard" = raise on failure
        
        Returns:
            OcrScanResult with findings or skip reason
        """
        # Preflight
        if not await self.health_check():
            if not await self.install():
                if required == "hard":
                    raise RuntimeError(
                        f"OCR required but unavailable after install attempt"
                    )
                return OcrScanResult(
                    success=False,
                    findings=[],
                    skipped_reason="OCR not available, scan skipped"
                )
        
        # Run scan
        proc = await asyncio.create_subprocess_exec(
            str(self._binary), "scan",
            "--path", ",".join(files),
            "--format", "json",
            cwd=self.repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode != 0:
            if required == "hard":
                raise RuntimeError(f"OCR scan failed: {stderr.decode()}")
            return OcrScanResult(
                success=False,
                findings=[],
                skipped_reason=f"OCR scan failed: {stderr.decode()[:200]}"
            )
        
        # Parse JSON output
        import json
        data = json.loads(stdout.decode())
        findings = [OcrFinding(**f) for f in data.get("findings", [])]
        
        return OcrScanResult(success=True, findings=findings)
```

## 4. Integration Points

### 4.1 spec_gate (Optional Pre-scan)

```python
# In langgraph_sdk/nodes/spec_gate.py

async def spec_gate_node(state: TaskState) -> TaskState:
    # ... existing spec gate logic ...
    
    # Step 8.5: OCR pre-scan (soft required)
    ocr = OcrService(repo_root=state.repo_root)
    result = await ocr.scan(files=state.files, required="soft")
    
    if result.skipped_reason:
        state.logs.append(f"[spec_gate] {result.skipped_reason}")
    elif result.findings:
        state.pre_scan_findings = result.findings
        state.task_body += format_pre_scan_section(result.findings)
    
    return state

def format_pre_scan_section(findings: list[OcrFinding]) -> str:
    lines = ["\n## Pre-scan findings (OCR)\n"]
    for f in findings:
        lines.append(f"- **{f.file}:{f.line}** [{f.severity}] {f.rule_id}: {f.message}")
    return "\n".join(lines)
```

### 4.2 review_order_gate (Mandatory When Declared)

```python
# In langgraph_sdk/nodes/review_order_gate.py

async def review_order_gate_node(state: TaskState) -> TaskState:
    # ... existing review order logic ...
    
    # Check if OCR is declared in toolchain
    toolchain = load_review_toolchain(state.repo_root)
    ocr_required = "ocr" in toolchain.tools
    
    if ocr_required:
        ocr = OcrService(repo_root=state.repo_root)
        result = await ocr.scan(
            files=state.changed_files,  # from result_ref diff
            required="hard"
        )
        
        if result.findings:
            state.review_sheet.ocr_findings = result.findings
    
    return state
```

### 4.3 Preflight Logic Integration

```python
# In langgraph_sdk/services/preflight.py

from enum import Enum

class PreflightResult(Enum):
    READY = "ready"
    SKIPPED = "skipped"
    BLOCKED = "blocked"

async def run_preflight(
    tool_id: str,
    repo_root: Path,
    required: str
) -> tuple[PreflightResult, str | None]:
    """
    Generic preflight runner following V1 algorithm.
    
    Returns:
        (result, message) - result is READY/SKIPPED/BLOCKED, message explains why
    """
    registry = load_tool_registry()
    tool = registry.get(tool_id)
    
    if not tool:
        return (PreflightResult.BLOCKED, f"Unknown tool: {tool_id}")
    
    # Step 1: Health check
    check_ok = await run_health_check(tool, repo_root)
    
    if not check_ok:
        # Step 2: Attempt install
        install_ok = await run_install(tool, repo_root)
        
        # Step 3: Re-check
        if install_ok:
            check_ok = await run_health_check(tool, repo_root)
    
    if not check_ok:
        if required == "hard":
            return (
                PreflightResult.BLOCKED,
                format_escalation(tool, repo_root)
            )
        else:
            return (
                PreflightResult.SKIPPED,
                f"Tool {tool_id} unavailable, step skipped"
            )
    
    return (PreflightResult.READY, None)
```

## 5. Recommendation

**PROCEED** with the proposed design.

### Rationale

1. **V1 Parity**: Maps cleanly to V1 concepts (PM pre-scan → spec_gate, Review toolchain → review_order_gate)
2. **Async-native**: Uses `asyncio.create_subprocess_exec` instead of blocking `subprocess.run`
3. **Structured output**: Pydantic models replace raw JSON parsing
4. **Preflight preserved**: Full V1 preflight algorithm (health_check → install → re-check → log/block) implemented
5. **Required semantics**: Respects soft/hard distinction per V1 registry

### Implementation Order

1. `OcrService` class with health_check/install/scan
2. `PreflightResult` enum and generic preflight runner
3. `spec_gate` integration (soft, optional)
4. `review_order_gate` integration (hard when declared)
5. Unit tests for each component

### Open Questions

- **Binary location**: Should V2 support a configurable binary path (for CI environments)?
- **Caching**: Should health_check result be cached per-session to avoid repeated version checks?
- **Timeout**: Should scan have a configurable timeout (V1 had no explicit timeout)?

These are implementation details that can be decided during development.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";

// ---------------------------------------------------------------------------
// agent-codebase-bench index-tool bridge.
//
// pi has no native MCP integration, so the four index-backed benchmark tools
// (codegraph / graft / repowise / codebase-memory-mcp) are re-exposed here as
// plain custom tools that shell out to each tool's CLI against a fixed kernel
// directory. Each registered name is ALSO the `-t <name>` allowlist token that
// the benchmark harness uses to isolate one tool per cell — the model can only
// call the tool(s) named on the command line, so measurements are not polluted
// by an accidental ripgrep/bash fallback.
//
// The KERNEL_DIR env var is where the tree + tool indexes live. It defaults to
// /workspace/linux to match the Docker image.
// ---------------------------------------------------------------------------

const KERNEL_DIR = process.env.KERNEL_DIR || "/workspace/linux";

interface RunResult {
  content: { type: "text"; text: string }[];
  details: Record<string, unknown>;
  isError?: boolean;
}

function run(bin: string, args: string[]): Promise<RunResult> {
  return new Promise((resolve) => {
    execFile(
      bin,
      args,
      { cwd: KERNEL_DIR, timeout: 300_000, maxBuffer: 64 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          const msg = (stderr || stdout || err.message).trim() || String(err);
          resolve({
            content: [{ type: "text", text: msg }],
            details: { exitCode: typeof (err as any).code === "number" ? (err as any).code : null },
            isError: true,
          });
          return;
        }
        resolve({ content: [{ type: "text", text: stdout }], details: {} });
      }
    );
  });
}

export default function (pi: ExtensionAPI) {
  // --- codegraph -----------------------------------------------------------
  pi.registerTool({
    name: "codegraph",
    label: "CodeGraph",
    description:
      "Query the codegraph symbol index of the codebase. Returns matching " +
      "symbols with their source, file:line locations, and call paths in one " +
      "shot. Use for any structural/code-query question about the codebase.",
    parameters: Type.Object({
      query: Type.String({ description: "The symbol / query to explore, e.g. 'drm_dev_register' or 'i915_pm_suspend callers'" }),
    }),
    async execute(_id, params) {
      return run("codegraph", ["explore", params.query, "--no-color"]);
    },
  });

  // --- graft ---------------------------------------------------------------
  pi.registerTool({
    name: "graft",
    label: "Graft",
    description:
      "Query the graft context graph of the codebase. Returns ranked, " +
      "relevant nodes with exact file:line references. Use for any " +
      "code-query / call-graph question about the codebase.",
    parameters: Type.Object({
      query: Type.String({ description: "The natural-language or symbol query, e.g. 'functions calling drm_dev_register'" }),
    }),
    async execute(_id, params) {
      return run("graft", ["ask", params.query]);
    },
  });

  // --- repowise ------------------------------------------------------------
  pi.registerTool({
    name: "repowise",
    label: "Repowise",
    description:
      "Search the repowise wiki / vector index of the codebase by keyword, " +
      "meaning, or symbol name. Returns pages with file:line references. " +
      "Use for any code-query question about the codebase.",
    parameters: Type.Object({
      query: Type.String({ description: "The keyword / meaning / symbol-name query, e.g. 'drm_dev_register' or 'i915_pm_suspend'" }),
    }),
    async execute(_id, params) {
      return run("repowise", ["search", params.query]);
    },
  });

  // --- codebase-memory-mcp -------------------------------------------------
  pi.registerTool({
    name: "codebase_memory",
    label: "Codebase Memory",
    description:
      "Run a codebase-memory-mcp tool against the indexed codebase. Pass the " +
      'tool name (e.g. "search_code", "get_code_snippet", "trace_path", ' +
      '"query_graph") and a JSON string of its arguments. Use for any ' +
      "structural / graph code-query question about the codebase.",
    parameters: Type.Object({
      tool: Type.String({ description: "MCP tool name: search_code, get_code_snippet, trace_path, query_graph, get_graph_schema, list_projects, get_architecture" }),
      arguments: Type.String({ description: "JSON object of tool arguments, e.g. '{\"query\": \"drm_dev_register\"}'" }),
    }),
    async execute(_id, params) {
      return run("codebase-memory-mcp", ["cli", params.tool, params.arguments]);
    },
  });

  // --- rtk (Rust Token Killer) --------------------------------------------
  pi.registerTool({
    name: "rtk",
    label: "RTK",
    description:
      "Run rtk grep (Rust Token Killer) over the codebase: returns grep-style " +
      "matches grouped by file with long lines truncated, so it cuts the " +
      "tokens you read. Use for any code-search / call-site question about " +
      "the codebase; rtk grep ~ rips a ripgrep search path (from the kernel " +
      "dir by default).",
    parameters: Type.Object({
      pattern: Type.String({ description: "The regex / substring to search for, e.g. 'drm_dev_register' or 'pm_runtime_get_sync'" }),
      path: Type.Optional(Type.String({ description: "Subdirectory to search (default: the kernel tree root)" })),
    }),
    async execute(_id, params) {
      return run("rtk", ["grep", params.pattern, params.path || "."]);
    },
  });

  // --- ripgrep / regrep built-ins ------------------------------------------
  // The built-in grep/find/ls/read/bash cover the `grep` and `ripgrep` cells;
  // they are allowlisted by the harness as `-t read,grep,find,ls[,bash]`. No
  // registration needed here (built-ins), but we keep the extension load so
  // one `-e bench-tools` enables all index tools plus the built-ins.
}

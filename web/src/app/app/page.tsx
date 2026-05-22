"use client";

import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import {
  Plus,
  MessageSquare,
  FolderClosed,
  Workflow,
  History as HistoryIcon,
  Library as LibraryIcon,
  Settings as SettingsIcon,
  LogOut,
  Globe,
  Database,
  Languages,
  Inbox,
  Menu,
  X,
  PanelRightClose,
  PanelRightOpen,
  Copy,
  Check,
  ArrowUpRight,
  Cpu,
  Mail,
  FileDown,
  PenLine,
  Sparkles,
  Scale,
  LayoutDashboard,
  FileText,
  Eye,
  Download,
  ChevronDown,
  Search,
} from "lucide-react";
import { PromptInputBox } from "@/components/ui/ai-prompt-box";
import { renderMarkdown } from "./markdown";
import VaultPane from "./vault-pane";
import WorkflowsPane from "./workflows-pane";
import HistoryPane from "./history-pane";
import LibraryPane from "./library-pane";
import CourtSearchPane from "./court-search-pane";
import SettingsPane from "./settings-pane";
import DashboardPane from "./dashboard-pane";
import ClientsPane from "./clients-pane";
import TemplatesPage from "./templates/page";
import ContractsPane from "./contracts-pane";
import EditorPane from "./editor-pane";

// Sanhita — India's largest AI legal research platform.
// 30M+ judgments from all 25 High Courts (1950–2025), FTS5-indexed.
const JURISDICTION = "IN";

// Source databases — FTS5 is the primary search engine over 30M+ records.
const SOURCES: { value: string; icon: string; label: string }[] = [
  { value: "", icon: "⚖️", label: "All courts" },
  { value: "fts5", icon: "🏛️", label: "Case law (30M+)" },
  { value: "legal_qa", icon: "❓", label: "Legal Q&A (1.3M)" },
  { value: "statutes", icon: "📜", label: "Statutes & Acts" },
];

// Model is fixed to Gemini — no picker shown to users.

const SUGGESTIONS: { q: string; tag: string }[] = [
  {
    tag: "Bail under NDPS Act",
    q: "What are the grounds for bail under Section 37 of the NDPS Act? Cite leading HC judgments.",
  },
  {
    tag: "Section 138 NI Act",
    q: "Standard of proof for cheque dishonour under Section 138 NI Act — recent High Court rulings.",
  },
  {
    tag: "Writ Petition Art. 226",
    q: "Grounds for filing a writ petition under Article 226 — when can a High Court issue certiorari?",
  },
  {
    tag: "Motor Accident Claims",
    q: "Formula for computing motor accident compensation — multiplier method vs structured formula.",
  },
];

type Mode =
  | "assistant"
  | "vault"
  | "workflows"
  | "court-search"
  | "history"
  | "library"
  | "clients"
  | "settings"
  | "dashboard"
  | "templates"
  | "editor"
  | "contracts";

interface LanguageOpt {
  code: string;
  label: string;
  native: string;
  family: string;
  rtl: boolean;
}

interface Thread {
  id: number;
  title: string;
  updated_at: number;
}

interface Citation {
  n: number;
  title: string;
  court?: string;
  year?: number | string;
  citation?: string;
  excerpt?: string;
  pdf_url?: string;
  url?: string;
  tier?: string;
  verdict?: string;
  judge?: string;
  score?: number;
  doc_type?: string; // "Judgment" | "Legal Document" | "Statute" | "Legal Q&A"
}

interface TraceStep {
  tool?: string;
  args?: Record<string, unknown>;
  result_preview?: string;
  ms?: number;
  error?: string;
}

interface WebCitation {
  title: string;
  url: string;
  source: string;
  source_name: string;
  excerpt: string;
  date?: string;
  relevance?: number;
}

interface Message {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  web_citations?: WebCitation[];
  llm?: { provider?: string; model?: string; latency_ms?: number };
  validation?: { confidence?: number; reasons?: string[] };
  refused?: boolean;
  trace?: TraceStep[];
  mode?: string;
  followups?: string[];
}

// Map URL hash to mode for deep-linking: /app#search → court-search
const HASH_TO_MODE: Record<string, Mode> = {
  "": "assistant",
  assistant: "assistant",
  search: "court-search",
  "court-search": "court-search",
  vault: "vault",
  storage: "vault",
  workflows: "workflows",
  history: "history",
  library: "library",
  clients: "clients",
  settings: "settings",
  dashboard: "dashboard",
  templates: "templates",
  editor: "contracts",
  contracts: "contracts",
  drafter: "contracts",
};
const MODE_TO_HASH: Record<Mode, string> = {
  assistant: "",
  "court-search": "search",
  vault: "vault",
  workflows: "workflows",
  history: "history",
  library: "library",
  clients: "clients",
  settings: "settings",
  dashboard: "dashboard",
  templates: "templates",
  editor: "editor",
  contracts: "contracts",
};

export default function AppPage() {
  const router = useRouter();

  // Read initial mode from URL hash (e.g., /app#search)
  const initialMode = (): Mode => {
    if (typeof window === "undefined") return "assistant";
    const hash = window.location.hash.replace("#", "");
    return HASH_TO_MODE[hash] || "assistant";
  };
  const [mode, _setMode] = useState<Mode>(initialMode);

  // Wrap setMode to also update the URL hash
  const setMode = useCallback((m: Mode) => {
    _setMode(m);
    const hash = MODE_TO_HASH[m];
    if (hash) {
      window.history.replaceState(null, "", `/app#${hash}`);
    } else {
      window.history.replaceState(null, "", "/app");
    }
  }, []);
  const [user, setUser] = useState<{ email?: string; name?: string } | null>(null);
  // Threads list is no longer rendered in the sidebar (History pane owns
  // that surface). We still keep `setThreads` to seed the list on boot and
  // bump titles after the first chat — the History pane refetches its own
  // search results on demand. The `threads` array itself is unused so we
  // mute the lint warning by tossing the read with a `void`.
  const [threads, setThreads] = useState<Thread[]>([]);
  void threads;
  const [activeThread, setActiveThread] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);

  // Active vault document — when set, chat routes to /api/vault/chat
  const [activeVaultDoc, setActiveVaultDoc] = useState<{ doc_id: number; filename: string; chunks: number } | null>(null);

  // Pinned case from Court Search — when set, every chat message includes
  // `pinned_case` in the body so the chat-v2 endpoint uses it as [C1].
  // Also unlocks the 6-button action panel above the composer (Citator,
  // Find similar, Apply, Distinguish, Draft, Brief).
  const [pinnedCase, setPinnedCase] = useState<{
    id: string;
    title: string;
    court?: string;
    year?: number;
    citation?: string;
    body_md?: string;
  } | null>(null);
  // Inline citator status result (from /api/cases/{id}/status) rendered
  // under the pinned-case chip when the user clicks the Citator action.
  const [citatorStatus, setCitatorStatus] = useState<{
    status: string; color: string; label: string; summary: string;
    stats: Record<string, number | null>;
    treating_cases: Array<{ case_id: string; title: string; treatment: string; para_no: number | null; context: string }>;
  } | null>(null);
  const [citatorLoading, setCitatorLoading] = useState(false);
  const [uploadingVault, setUploadingVault] = useState(false);
  const [thinking, setThinking] = useState(false);
  // Live "what's happening right now" phases shown under the answer
  // bubble while we wait for the API. Each phase is a short ChatGPT-style
  // status line ("Searching case law…", "Drafting memo…"). The handler
  // advances them on a timer so the user sees motion even though the
  // backend isn't streaming.
  const [thinkingPhases, setThinkingPhases] = useState<string[]>([]);
  // Sync mode with URL hash for back/forward navigation
  useEffect(() => {
    const onHash = () => {
      const hash = window.location.hash.replace("#", "");
      const m = HASH_TO_MODE[hash] || "assistant";
      _setMode(m);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const jurisdiction = JURISDICTION; // India-only — no dropdown needed
  const [source, setSource] = useState("");
  const [connectors, setConnectors] = useState<Record<string, boolean>>({});
  // Output language for the AI's reply. Persisted in localStorage so a
  // Hindi-speaking user keeps Hindi across sessions. Empty string = English
  // (we never send an "en" code; null/empty round-trips as default).
  const [language, setLanguage] = useState<string>(
    typeof window !== "undefined"
      ? window.localStorage.getItem("sanhita.lang") || ""
      : ""
  );
  const [languages, setLanguages] = useState<LanguageOpt[]>([]);
  // Model is fixed — no user picker.
  const model = "";
  // Count of `new` NyayaSathi leads — drives the sidebar badge.
  const [newClientCount, setNewClientCount] = useState(0);
  // Mobile drawer state. Sidebar is permanent on >= md; on phone it slides
  // in over the chat. Citations rail is permanent on >= lg; on tablet it
  // toggles via a button in the topbar so the chat column gets full width.
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [railOpen, setRailOpen] = useState(false);
  const chatLogRef = useRef<HTMLDivElement>(null);

  // ── boot: load threads + connectors. 401 ⇒ /login.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/brief/threads", { credentials: "same-origin" });
        if (r.status === 401) {
          router.replace("/login");
          return;
        }
        const data = await r.json();
        if (cancelled) return;
        setThreads(data.threads || []);
        setUser(data.user || null);
      } catch {
        router.replace("/login");
      }
      try {
        const r = await fetch("/api/connectors");
        const data = await r.json();
        if (!cancelled) setConnectors(data.connectors || {});
      } catch {
        /* connector status is decorative */
      }
      // Load language catalog from the backend (single source of truth —
      // adding a language only requires extending brief_service.LANGUAGES).
      try {
        const r = await fetch("/api/languages");
        const data = await r.json();
        if (!cancelled) setLanguages(data.languages || []);
      } catch {
        /* fall back to English-only display */
      }
      // Initial NyayaSathi inbox count (drives sidebar badge).
      try {
        const r = await fetch("/api/clients?status=new", {
          credentials: "same-origin",
        });
        if (r.ok) {
          const data = await r.json();
          if (!cancelled) setNewClientCount(data.counts?.new ?? 0);
        }
      } catch {
        /* badge is decorative */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  // Auto-scroll the chat log on new messages
  useEffect(() => {
    chatLogRef.current?.scrollTo({
      top: chatLogRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, thinking]);

  const newThread = useCallback(async (): Promise<number | null> => {
    try {
      const r = await fetch("/api/brief/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: "{}",
      });
      if (!r.ok) return null;
      const data = await r.json();
      const t: Thread = data.thread;
      setThreads((prev) => [t, ...prev]);
      setActiveThread(t.id);
      setMessages([]);
      return t.id;
    } catch {
      return null;
    }
  }, []);

  const openThread = useCallback(async (id: number) => {
    setActiveThread(id);
    setMessages([]);
    try {
      const r = await fetch(`/api/brief/threads/${id}`, { credentials: "same-origin" });
      if (!r.ok) return;
      const data = await r.json();
      const msgs: Message[] = (data.messages || []).map((m: { role: string; content: string; citations?: string }) => ({
        role: m.role as "user" | "assistant",
        content: m.content,
        citations: m.citations ? safeParse(m.citations) : undefined,
      }));
      setMessages(msgs);
    } catch {
      /* silent */
    }
  }, []);

  const handleVaultUpload = useCallback(async (file: File) => {
    setUploadingVault(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const r = await fetch("/api/vault/upload", {
        method: "POST",
        credentials: "same-origin",
        body: form,
      });
      if (!r.ok) throw new Error(`Upload failed: ${r.status}`);
      const d = await r.json() as { ok: boolean; doc_id: number; filename: string; chunks: number };
      if (d.ok) {
        setActiveVaultDoc({ doc_id: d.doc_id, filename: d.filename, chunks: d.chunks });
      }
    } catch (e) {
      console.error("vault upload failed:", e);
    } finally {
      setUploadingVault(false);
    }
  }, []);

  const handleSend = useCallback(
    async (
      message: string,
      _files?: File[],
      opts?: { search?: boolean; think?: boolean; canvas?: boolean; agent?: boolean }
    ) => {
      const text = message.trim();
      if (!text) return;

      let tid = activeThread;
      if (!tid) {
        tid = await newThread();
        if (!tid) return;
      }

      setMessages((prev) => [...prev, { role: "user", content: text }]);
      setThinking(true);

      // Mode routing — Harvey-style action toggles.
      //   Vault   → /api/vault/chat   (Q&A over uploaded document)
      //   Agent   → /api/brief/agent (Gemini chains tools across turns)
      //   Canvas  → /api/brief/draft  (open drafting, no retrieval)
      //   Search  → /api/brief/web    (real web search + grounded answer)
      //   else    → /api/brief/chat-v2 (planner → multi-corpus retrieve →
      //                synthesiser → answer-gate validator; spans 83M rows)
      const endpoint = activeVaultDoc && !opts?.agent && !opts?.canvas && !opts?.search
        ? "/api/vault/chat"
        : opts?.agent
        ? "/api/brief/agent"
        : opts?.canvas
        ? "/api/brief/draft"
        : opts?.search
        ? "/api/brief/web"
        : "/api/brief/chat-v2";

      // Live "thinking" phases — ChatGPT-style status under the bubble.
      // The backend isn't streaming, so we advance through plausible
      // phases on a timer. The phases mirror what the server is actually
      // doing in that mode (retrieve → draft → validate, or just draft
      // for canvas, or web fetch for search). Stops when the response
      // lands.
      const phasesByMode: Record<string, { ms: number; label: string }[]> =
        opts?.agent
          ? {
              agent: [
                { ms: 0, label: "Planning the agent loop…" },
                { ms: 1200, label: "Searching case law…" },
                { ms: 4000, label: "Pulling statutes…" },
                { ms: 7000, label: "Composing answer with citations…" },
                { ms: 11000, label: "Validating sources…" },
              ],
            }
          : opts?.canvas
          ? {
              draft: [
                { ms: 0, label: "Reading your request…" },
                { ms: 800, label: "Drafting with Gemini Flash…" },
                { ms: 4500, label: "Polishing structure…" },
              ],
            }
          : opts?.search
          ? {
              web: [
                { ms: 0, label: "Searching the open web…" },
                { ms: 1200, label: "Reading top results…" },
                { ms: 3000, label: "Composing grounded answer…" },
                { ms: 7000, label: "Mapping citations to URLs…" },
              ],
            }
          : {
              chat: [
                { ms: 0, label: "Understanding your query…" },
                { ms: 800, label: "Preparing response…" },
                { ms: 3000, label: "Composing answer…" },
              ],
            };
      const phases = Object.values(phasesByMode)[0]!;
      setThinkingPhases([phases[0].label]);
      const phaseTimers = phases.slice(1).map((p) =>
        setTimeout(() => {
          setThinkingPhases((prev) => [...prev, p.label]);
        }, p.ms)
      );

      // Source allowlist only applies to research mode.
      let srcs: string[] | null = null;
      if (!opts?.canvas && !opts?.search && !opts?.agent && source) {
        srcs = source.split(",").map((s) => s.trim()).filter(Boolean);
      }

      try {
        const r = await fetch(endpoint, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            thread_id: tid,
            question: text,
            jurisdiction,
            sources: srcs,
            language: language || undefined,
            // `prefer` matches llm.router.generate's prefer kwarg —
            // "gemini" | "anthropic" | "groq" | "cloudflare" reorder the
            // chain to put that provider first. Empty string = router
            // default (Gemini Flash).
            prefer: model || undefined,
            // vault doc routing — only sent when an uploaded doc is active
            ...(activeVaultDoc ? { doc_id: activeVaultDoc.doc_id } : {}),
            // pinned case — backend uses it as [C1] anchor + bypasses
            // retrieval when present (chat-v2 pinned branch).
            ...(pinnedCase ? {
              pinned_case: {
                id: pinnedCase.id,
                title: pinnedCase.title,
                court: pinnedCase.court,
                year: pinnedCase.year,
                citation: pinnedCase.citation,
                body_md: pinnedCase.body_md,
              }
            } : {}),
          }),
        });
        const data = await r.json();
        phaseTimers.forEach(clearTimeout);
        setThinkingPhases([]);
        setThinking(false);
        if (!r.ok) {
          setMessages((prev) => [
            ...prev,
            {
              role: "assistant",
              content: `**Error:** ${data.detail || `HTTP ${r.status}`}`,
            },
          ]);
          return;
        }
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: data.answer_markdown,
            citations: data.citations,
            web_citations: data.web_citations,
            llm: data.llm,
            validation: data.validation,
            refused: !!data.refused,
            trace: data.trace,
            mode: data.mode,
            followups: data.followups,
          },
        ]);
        // Bump the thread title if it was a fresh "New matter".
        setThreads((prev) =>
          prev.map((t) =>
            t.id === tid && (!t.title || t.title === "New matter")
              ? { ...t, title: text.slice(0, 48) }
              : t
          )
        );
      } catch (e) {
        phaseTimers.forEach(clearTimeout);
        setThinkingPhases([]);
        setThinking(false);
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: `**Network error.** ${(e as Error).message}`,
          },
        ]);
      }
    },
    [activeThread, jurisdiction, source, language, model, newThread, activeVaultDoc]
  );

  // Persist the language picker value across sessions. Keeping it on the
  // window/localStorage layer (not just React state) means a returning
  // Tamil-speaking user lands on Tamil without having to re-pick.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (language) window.localStorage.setItem("sanhita.lang", language);
    else window.localStorage.removeItem("sanhita.lang");
  }, [language]);

  // Model is fixed — no persistence needed.

  const lastCitations = useMemo<Citation[]>(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "assistant" && messages[i].citations?.length) {
        return messages[i].citations!;
      }
    }
    return [];
  }, [messages]);

  return (
    <div className="flex h-[100dvh] w-screen bg-[var(--bg)] overflow-hidden">
      {/* Mobile backdrop — only renders when the drawer is open on phones. */}
      {sidebarOpen && (
        <div
          className="md:hidden fixed inset-0 bg-black/40 z-30"
          onClick={() => setSidebarOpen(false)}
          aria-hidden
        />
      )}

      {/* ── Sidebar ─────────────────────────────────────────────────────── */}
      <aside
        className={`flex flex-col bg-[var(--bg-elev)] border-r border-[var(--line)] min-w-0 z-40 transition-transform duration-200
          fixed md:static inset-y-0 left-0 w-[260px] md:w-[230px] shrink-0
          ${sidebarOpen ? "translate-x-0" : "-translate-x-full"} md:translate-x-0`}
      >
        <div className="px-6 pt-7 pb-6 flex items-start justify-between">
          <div>
            <div className="font-display text-3xl tracking-tight text-[var(--ink)]">Sanhita</div>
            <div className="text-[10px] tracking-[0.22em] uppercase text-[var(--ink-soft)] mt-1">
              Research counsel
            </div>
          </div>
          <button
            className="md:hidden p-1.5 rounded-lg hover:bg-[var(--bg)] transition-colors text-[var(--ink-soft)]"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close menu"
          >
            <X size={18} />
          </button>
        </div>

        <button
          onClick={() => {
            setMode("assistant");
            newThread();
            setSidebarOpen(false);
          }}
          className="mx-4 mb-4 flex items-center gap-2 rounded-xl py-2.5 px-4 text-sm font-medium bg-[var(--ink)] text-[var(--bg)] hover:opacity-90 transition-opacity"
        >
          <Plus size={16} strokeWidth={2.4} />
          New matter
        </button>

        <nav className="px-2 flex flex-col gap-0.5" role="navigation" aria-label="Main navigation">
          <SideItem href="/app" icon={<MessageSquare size={16} />} label="Assistant" active={mode === "assistant"} onClick={() => { setMode("assistant"); setSidebarOpen(false); }} />
          <SideItem href="/app#workflows" icon={<Workflow size={16} />} label="Workflows" active={mode === "workflows"} onClick={() => { setMode("workflows"); setSidebarOpen(false); }} />
          <SideItem href="/app#search" icon={<Scale size={16} />} label="Court Search" active={mode === "court-search"} onClick={() => { setMode("court-search"); setSidebarOpen(false); }} />
          {/* Drafter = template-driven contract / pleading drafter (slots → MD). */}
          <SideItem href="/app#drafter" icon={<PenLine size={16} />} label="Drafter" active={mode === "contracts"} onClick={() => { setMode("contracts"); setSidebarOpen(false); }} />
          {/* Editor = free-form rich-text editor (TipTap) for refining drafts,
              redlining, AI-assist, and Export menu. Workflows' "Send to Editor"
              lands here via sessionStorage handoff. */}
          <SideItem href="/app#editor" icon={<FileText size={16} />} label="Editor" active={mode === "editor"} onClick={() => { setMode("editor"); setSidebarOpen(false); }} />
          <SideItem href="/app#clients" icon={<Inbox size={16} />} label="Clients" active={mode === "clients"} onClick={() => { setMode("clients"); setSidebarOpen(false); }} badge={newClientCount > 0 ? newClientCount : undefined} />
          <SideItem href="/app#history" icon={<HistoryIcon size={16} />} label="History" active={mode === "history"} onClick={() => { setMode("history"); setSidebarOpen(false); }} />
          <SideItem href="/app#settings" icon={<SettingsIcon size={16} />} label="Settings" active={mode === "settings"} onClick={() => { setMode("settings"); setSidebarOpen(false); }} />
        </nav>

        <div className="flex-1" />

        <div className="mt-auto p-4 border-t border-[var(--line)]">
          <div className="text-xs text-[var(--ink-soft)] truncate" title={user?.email}>
            {user?.name || user?.email || "—"}
          </div>
          <a
            href="/api/logout"
            className="mt-2 flex items-center gap-2 text-xs text-[var(--ink-soft)] hover:text-[var(--ink)] transition-colors"
          >
            <LogOut size={13} /> Sign out
          </a>
        </div>
      </aside>

      {/* ── Main column ─────────────────────────────────────────────────── */}
      <main className="flex flex-col flex-1 min-w-0 paper-grain overflow-hidden">
        {/* Topbar with jurisdiction + source pickers — flex-wrap so the
            selects fall to a second row on narrow viewports rather than
            pushing the whole layout horizontally. */}
        <div className="flex items-center justify-between gap-2 flex-wrap border-b border-[var(--line)] px-3 sm:px-6 py-3 min-w-0">
          <div className="flex items-center gap-2 sm:gap-3 min-w-0">
            {/* Hamburger — only on phone (sidebar is a drawer there). */}
            <button
              className="md:hidden p-1.5 rounded-lg text-[var(--ink-soft)] hover:bg-[var(--bg-elev)] shrink-0"
              onClick={() => setSidebarOpen(true)}
              aria-label="Open menu"
            >
              <Menu size={20} />
            </button>
            <h1 className="font-display text-lg tracking-tight capitalize truncate">
              {modeTitle(mode)}
            </h1>
          </div>

          {mode === "assistant" && (
            <div className="flex items-center gap-2 min-w-0">
              {/* Database source selector */}
              <label className="inline-flex items-center gap-1.5 bg-[var(--bg-elev)] border border-[var(--line)] rounded-lg px-2.5 py-1.5 cursor-pointer hover:border-[var(--accent)] transition-colors" title="Source database">
                <Database size={13} className="text-[var(--accent)] shrink-0" />
                <select
                  value={source}
                  onChange={(e) => setSource(e.target.value)}
                  className="bg-transparent text-xs text-[var(--ink)] outline-none cursor-pointer appearance-none pr-3"
                >
                  {SOURCES.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
                <ChevronDown size={10} className="text-[var(--ink-soft)] -ml-2 shrink-0" />
              </label>

              {/* Language selector */}
              {languages.length > 0 && (
                <label className="inline-flex items-center gap-1.5 bg-[var(--bg-elev)] border border-[var(--line)] rounded-lg px-2.5 py-1.5 cursor-pointer hover:border-[var(--accent)] transition-colors" title="Reply language">
                  <Languages size={13} className="text-[var(--ink-soft)] shrink-0" />
                  <select
                    value={language}
                    onChange={(e) => setLanguage(e.target.value)}
                    className="bg-transparent text-xs text-[var(--ink)] outline-none cursor-pointer appearance-none pr-3"
                  >
                    {languages.map((l) => (
                      <option key={l.code} value={l.code === "en" ? "" : l.code}>
                        {l.native}
                      </option>
                    ))}
                  </select>
                  <ChevronDown size={10} className="text-[var(--ink-soft)] -ml-2 shrink-0" />
                </label>
              )}

              {/* Sources / Citations toggle */}
              <button
                className="inline-flex items-center gap-1.5 bg-[var(--bg-elev)] border border-[var(--line)] rounded-lg px-2.5 py-1.5 hover:border-[var(--accent)] transition-colors text-xs text-[var(--ink)]"
                onClick={() => setRailOpen((v) => !v)}
                aria-label="Toggle citations"
                title="Show sources / citations"
              >
                {railOpen ? <PanelRightClose size={13} /> : <PanelRightOpen size={13} />}
                <span className="hidden sm:inline">
                  {lastCitations.length > 0
                    ? `${lastCitations.length} Sources`
                    : "Sources"}
                </span>
              </button>
            </div>
          )}
        </div>

        {/* Body — switches by mode */}
        {mode === "assistant" && (
          <AssistantPane
            messages={messages}
            thinking={thinking}
            thinkingPhases={thinkingPhases}
            onSend={handleSend}
            chatLogRef={chatLogRef}
            citations={lastCitations}
            suggestions={SUGGESTIONS}
            railOpen={railOpen}
            onCloseRail={() => setRailOpen(false)}
            activeVaultDoc={activeVaultDoc}
            uploadingVault={uploadingVault}
            onVaultUpload={handleVaultUpload}
            onClearVaultDoc={() => setActiveVaultDoc(null)}
            onOpenInEditor={(content) => {
              if (typeof window !== "undefined") {
                window.sessionStorage.setItem("editor_draft_content", content);
                window.sessionStorage.setItem("editor_draft_title", "Research Note");
              }
              setMode("editor");
            }}
          />
        )}
        {mode === "vault" && <VaultPane />}
        {mode === "workflows" && (
          <WorkflowsPane
            onOpenInEditor={(content: string) => {
              // Store draft content in sessionStorage for EditorPane to pick up
              if (typeof window !== "undefined") {
                window.sessionStorage.setItem("editor_draft_content", content);
              }
              setMode("editor");
            }}
          />
        )}
        {mode === "court-search" && (
          <CourtSearchPane
            onUseInChat={async (c) => {
              const tid = activeThread || (await newThread());
              if (!tid) return;
              setMode("assistant");
              // Pin the case for backend grounding + action panel. The
              // composer chip + 6 action buttons render off `pinnedCase`.
              setPinnedCase({
                id: c.case_id || "",
                title: c.title || "Untitled case",
                body_md: c.body_md,
              });
              setCitatorStatus(null);
              const seed = `📌 Pinned: **${c.title || "case"}**. Use the action buttons below or ask anything about this case.`;
              setMessages((prev) => [
                ...prev,
                { role: "assistant", content: seed },
              ]);
            }}
          />
        )}
        {mode === "clients" && (
          <ClientsPane
            onOpenThread={async (id) => {
              setMode("assistant");
              await openThread(id);
              // Refresh badge — the client we just opened has flipped to
              // in_progress, so the "new" count drops by one.
              try {
                const r = await fetch("/api/clients?status=new", {
                  credentials: "same-origin",
                });
                if (r.ok) {
                  const data = await r.json();
                  setNewClientCount(data.counts?.new ?? 0);
                }
              } catch {
                /* badge is decorative */
              }
            }}
          />
        )}
        {mode === "history" && (
          <HistoryPane
            onOpenThread={(id) => {
              setMode("assistant");
              openThread(id);
            }}
          />
        )}
        {mode === "library" && (
          <LibraryPane
            onUseInChat={async (doc) => {
              const tid = activeThread || (await newThread());
              if (!tid) return;
              setMode("assistant");
              const seed = `Use this ${doc.kind} as context:\n\n**${doc.title}**\n\n${doc.body_md}\n\nAnalyze this document and help me with: `;
              setMessages((prev) => [
                ...prev,
                { role: "assistant", content: seed },
              ]);
            }}
          />
        )}
        {mode === "settings" && (
          <SettingsPane
            onChange={async () => {
              try {
                const r = await fetch("/api/connectors");
                const data = await r.json();
                setConnectors(data.connectors || {});
              } catch {
                /* connector status is decorative */
              }
            }}
          />
        )}
        {mode === "templates" && <TemplatesPage />}
        {mode === "contracts" && <ContractsPane />}
        {mode === "editor"   && <EditorPane />}
        {mode === "dashboard" && (
          <DashboardPane
            // "Ask Sanhita" hands the assistant a snapshot of the current
            // dashboard state so the next answer is grounded in real
            // numbers ("you have 1,135 cases indexed; 2 admins online").
            onAskSanhita={async (seedPrompt) => {
              const tid = activeThread || (await newThread());
              if (!tid) return;
              setMode("assistant");
              setMessages((prev) => [
                ...prev,
                { role: "assistant", content: seedPrompt },
              ]);
            }}
          />
        )}
      </main>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────

function SideItem({
  href,
  icon,
  label,
  active,
  onClick,
  badge,
}: {
  href?: string;
  icon: React.ReactNode;
  label: string;
  active?: boolean;
  onClick: () => void;
  badge?: number;
}) {
  return (
    <a
      href={href || "#"}
      onClick={(e) => { e.preventDefault(); onClick(); }}
      className={`flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm transition-colors text-left ${
        active
          ? "bg-[var(--highlight)] text-[var(--ink)] font-medium"
          : "text-[var(--ink-soft)] hover:bg-[var(--bg)] hover:text-[var(--ink)]"
      }`}
      aria-current={active ? "page" : undefined}
    >
      <span className={active ? "text-[var(--accent)]" : "text-[var(--ink-soft)]"}>{icon}</span>
      <span className="flex-1">{label}</span>
      {badge !== undefined && badge > 0 && (
        <span className="text-[10px] rounded-full px-1.5 py-0.5 min-w-[18px] text-center font-mono bg-[var(--accent)] text-white">
          {badge > 99 ? "99+" : badge}
        </span>
      )}
    </a>
  );
}

function modeTitle(mode: Mode): string {
  switch (mode) {
    case "assistant":
      return "Assistant";
    case "vault":
      return "Storage";
    case "workflows":
      return "Workflows";
    case "court-search":
      return "Court Search";
    case "clients":
      return "Clients";
    case "history":
      return "History";
    case "library":
      return "Library";
    case "settings":
      return "Settings";
    case "dashboard":
      return "Dashboard";
    case "templates":
      return "Templates";
    case "editor":
      return "Draft Editor";
    default:
      return "Sanhita";
  }
}

// ── Assistant pane: chat log + citations rail + composer ───────────────────
function AssistantPane({
  messages,
  thinking,
  thinkingPhases,
  onSend,
  chatLogRef,
  citations,
  suggestions,
  railOpen,
  onCloseRail,
  onOpenInEditor,
  activeVaultDoc,
  uploadingVault,
  onVaultUpload,
  onClearVaultDoc,
}: {
  messages: Message[];
  thinking: boolean;
  thinkingPhases: string[];
  onSend: (m: string, files?: File[], opts?: { search?: boolean; think?: boolean; canvas?: boolean }) => void;
  chatLogRef: React.RefObject<HTMLDivElement | null>;
  citations: Citation[];
  suggestions: { q: string; tag: string }[];
  railOpen: boolean;
  onCloseRail: () => void;
  onOpenInEditor?: (content: string) => void;
  activeVaultDoc?: { doc_id: number; filename: string; chunks: number } | null;
  uploadingVault?: boolean;
  onVaultUpload?: (file: File) => void;
  onClearVaultDoc?: () => void;
}) {
  const empty = messages.length === 0;
  const fileInputRef = useRef<HTMLInputElement>(null);

  return (
    // Single-column on phone/tablet (< lg), two-column with permanent rail
    // on lg+. The rail becomes a slide-over drawer on smaller viewports —
    // toggled from the topbar button.
    <div className="flex flex-1 min-h-0 min-w-0 relative">
      {/* Chat column */}
      <section className="flex flex-col flex-1 min-w-0 min-h-0">
        <div ref={chatLogRef} className="flex-1 overflow-y-auto px-4 sm:px-6 lg:px-12 py-6 sm:py-8 min-w-0">
          {empty ? (
            <EmptyState onPick={(q) => onSend(q)} suggestions={suggestions} />
          ) : (
            <div className="max-w-3xl mx-auto flex flex-col gap-6 min-w-0">
              {messages.map((m, i) => (
                <ChatBubble key={i} m={m} onPickFollowup={(q) => onSend(q)} onOpenInEditor={onOpenInEditor} />
              ))}
              {thinking && <ThinkingPanel phases={thinkingPhases} />}
            </div>
          )}
        </div>

        {/* Composer — compact on phone, generous on desktop. The
            safe-area-inset keeps the send button above the iPhone home bar. */}
        <div
          className="px-3 sm:px-6 lg:px-12 pt-3 sm:pt-4 border-t border-[var(--line)] bg-[var(--bg)] min-w-0"
          style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}
        >
          {/* Hidden file input for vault upload */}
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf,.docx,.txt"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f && onVaultUpload) onVaultUpload(f);
              e.target.value = "";
            }}
          />
          {/* Pinned case panel — appears when a Court Search result is
              pinned. Chip + 6 action buttons (Citator wired; others
              shipped in subsequent commits). Clicking ✕ unpins. */}
          {pinnedCase && (
            <div className="max-w-3xl mx-auto mb-2 space-y-2">
              <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 text-xs">
                <span>📌</span>
                <span className="font-medium truncate min-w-0">{pinnedCase.title}</span>
                {pinnedCase.citation && (
                  <span className="text-[var(--ink-soft)] shrink-0">· {pinnedCase.citation}</span>
                )}
                <button
                  onClick={() => { setPinnedCase(null); setCitatorStatus(null); }}
                  className="ml-auto text-[var(--ink-soft)] hover:text-[var(--ink)] shrink-0"
                  title="Unpin case"
                >✕</button>
              </div>

              {/* 6 action cards. Citator is wired; the other five seed
                  a pre-baked question into the composer (and the
                  pinned_case context flows through to backend). */}
              <div className="flex flex-wrap gap-1.5">
                <button
                  onClick={async () => {
                    if (!pinnedCase.id) return;
                    setCitatorLoading(true);
                    setCitatorStatus(null);
                    try {
                      const r = await fetch(`/api/cases/${encodeURIComponent(pinnedCase.id)}/status`, { credentials: "same-origin" });
                      if (r.ok) setCitatorStatus(await r.json());
                      else setCitatorStatus({ status: "error", color: "grey", label: "Unavailable", summary: `HTTP ${r.status} — citator data not found for this case.`, stats: {}, treating_cases: [] });
                    } catch (e) {
                      setCitatorStatus({ status: "error", color: "grey", label: "Unavailable", summary: String(e), stats: {}, treating_cases: [] });
                    } finally {
                      setCitatorLoading(false);
                    }
                  }}
                  className="text-xs px-2.5 py-1 rounded-lg border border-[var(--line)] bg-[var(--bg-elev)] hover:bg-[var(--bg-hover)] disabled:opacity-50"
                  disabled={citatorLoading}
                >⚖️ Citator{citatorLoading ? "…" : ""}</button>
                <button
                  onClick={() => onSend(`Brief this case in 6 bullets: parties, facts, issues, ratio, key citations, outcome.`)}
                  className="text-xs px-2.5 py-1 rounded-lg border border-[var(--line)] bg-[var(--bg-elev)] hover:bg-[var(--bg-hover)]"
                >📄 Brief</button>
                <button
                  onClick={() => onSend(`Find 5 similar Indian cases to this one, focusing on the same legal issue, with citations.`)}
                  className="text-xs px-2.5 py-1 rounded-lg border border-[var(--line)] bg-[var(--bg-elev)] hover:bg-[var(--bg-hover)]"
                >🔍 Similar</button>
                <button
                  onClick={() => onSend(`Apply the ratio of this case to my matter. My facts: `)}
                  className="text-xs px-2.5 py-1 rounded-lg border border-[var(--line)] bg-[var(--bg-elev)] hover:bg-[var(--bg-hover)]"
                >🎯 Apply</button>
                <button
                  onClick={() => onSend(`Distinguish this case from my matter. My facts differ as follows: `)}
                  className="text-xs px-2.5 py-1 rounded-lg border border-[var(--line)] bg-[var(--bg-elev)] hover:bg-[var(--bg-hover)]"
                >⚔️ Distinguish</button>
                <button
                  onClick={() => onSend(`Draft a 3-paragraph submission using this case as the primary authority. Issue: `)}
                  className="text-xs px-2.5 py-1 rounded-lg border border-[var(--line)] bg-[var(--bg-elev)] hover:bg-[var(--bg-hover)]"
                >✍️ Draft</button>
              </div>

              {/* Citator status panel — only when /status has returned. */}
              {citatorStatus && (
                <div className={`rounded-xl border px-3 py-2 text-xs ${
                  citatorStatus.color === "red" ? "border-red-300 bg-red-50 dark:bg-red-950/30" :
                  citatorStatus.color === "amber" ? "border-amber-300 bg-amber-50 dark:bg-amber-950/30" :
                  citatorStatus.color === "green" ? "border-emerald-300 bg-emerald-50 dark:bg-emerald-950/30" :
                  "border-[var(--line)] bg-[var(--bg-elev)]"
                }`}>
                  <div className="flex items-center gap-2 font-medium">
                    <span>{
                      citatorStatus.color === "red" ? "🛑" :
                      citatorStatus.color === "amber" ? "⚠️" :
                      citatorStatus.color === "green" ? "✅" : "ℹ️"
                    }</span>
                    <span>{citatorStatus.label}</span>
                    <button
                      onClick={() => setCitatorStatus(null)}
                      className="ml-auto text-[var(--ink-soft)] hover:text-[var(--ink)]"
                    >✕</button>
                  </div>
                  <div className="mt-1 text-[var(--ink-soft)]">{citatorStatus.summary}</div>
                  {citatorStatus.treating_cases && citatorStatus.treating_cases.length > 0 && (
                    <div className="mt-2 space-y-1">
                      <div className="text-[10px] uppercase tracking-wide text-[var(--ink-soft)]">Treating cases</div>
                      {citatorStatus.treating_cases.slice(0, 5).map((tc, j) => (
                        <div key={j} className="flex items-start gap-2">
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-[var(--bg)] border border-[var(--line)] shrink-0 mt-0.5">
                            {tc.treatment}
                          </span>
                          <span className="truncate min-w-0">{tc.title || tc.case_id}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          {/* Active vault doc chip */}
          {activeVaultDoc && (
            <div className="max-w-3xl mx-auto mb-2 flex items-center gap-2">
              <div className="flex-1 flex items-center gap-2 px-3 py-1.5 rounded-xl bg-[var(--accent)]/10 border border-[var(--accent)]/30 text-xs">
                <span className="text-[var(--accent)]">📄</span>
                <span className="text-[var(--ink)] font-medium truncate min-w-0">
                  {activeVaultDoc.filename}
                </span>
                <span className="text-[var(--ink-soft)] shrink-0">
                  · {activeVaultDoc.chunks} chunks · asking from this doc
                </span>
                <button
                  onClick={onClearVaultDoc}
                  className="ml-auto text-[var(--ink-soft)] hover:text-[var(--ink)] shrink-0"
                  title="Remove document"
                >
                  ✕
                </button>
              </div>
            </div>
          )}
          {/* Upload progress */}
          {uploadingVault && (
            <div className="max-w-3xl mx-auto mb-2">
              <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-[var(--bg-elev)] border border-[var(--line)] text-xs text-[var(--ink-soft)]">
                <div className="w-3 h-3 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin shrink-0" />
                Uploading document…
              </div>
            </div>
          )}
          <div className="max-w-3xl mx-auto min-w-0">
            <PromptInputBox
              onSend={onSend}
              isLoading={thinking}
              placeholder="Ask Sanhita"
            />
          </div>
        </div>
      </section>

      {/* Backdrop — only renders while the rail drawer is open. Click
          outside to dismiss. */}
      {railOpen && (
        <div
          className="fixed inset-0 bg-black/40 z-30"
          onClick={onCloseRail}
          aria-hidden
        />
      )}

      {/* Citations rail — always a slide-over drawer. Hidden by default on
          every viewport; opens when the user clicks the Sources button in
          the topbar. overflow-x-hidden so long titles wrap instead of
          pushing the column wider than its track. */}
      <aside
        className={`border-l border-[var(--line)] bg-[var(--bg-elev)] overflow-y-auto overflow-x-hidden min-w-0
          fixed inset-y-0 right-0 w-[88vw] sm:w-[340px] z-40 transition-transform duration-200 shadow-xl
          ${railOpen ? "translate-x-0" : "translate-x-full"}`}
      >
        <div className="px-5 py-4 border-b border-[var(--line)] min-w-0 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="text-[10px] tracking-[0.22em] uppercase text-[var(--ink-soft)]">
              Sources
            </div>
            <div className="font-display text-lg tracking-tight">
              {citations.length ? `${citations.length} citation${citations.length === 1 ? "" : "s"}` : "Citations"}
            </div>
          </div>
          <button
            className="p-1 rounded-md text-[var(--ink-soft)] hover:bg-[var(--bg)] shrink-0"
            onClick={onCloseRail}
            aria-label="Close citations"
          >
            <X size={16} />
          </button>
        </div>

        <div className="px-3 py-3 flex flex-col gap-2 min-w-0">
          {citations.length === 0 && (
            <p className="text-sm italic text-[var(--ink-soft)] px-2">
              Citations from your latest answer will appear here.
            </p>
          )}
          {citations.map((c) => (
            <SourceCard key={c.n} c={c} />
          ))}
        </div>
      </aside>
    </div>
  );
}

// Live "what's happening right now" panel — ChatGPT-style. Each entry
// in `phases` was appended on a timer by the handler, so the list grows
// as the request progresses. The most recent phase has a pulsing dot
// (in-flight); earlier phases get a static check (done).
function ThinkingPanel({ phases }: { phases: string[] }) {
  return (
    <div className="self-start w-full sm:max-w-[92%] flex flex-col gap-2 min-w-0">
      <div className="text-[10px] tracking-[0.22em] uppercase text-[var(--ink-soft)] flex items-center gap-2">
        <Sparkles size={11} className="text-[var(--accent)]" />
        Thinking
      </div>
      <div className="bg-[var(--bg-elev)] border border-[var(--line)] rounded-2xl rounded-bl-sm px-5 py-3 min-w-0">
        <ul className="flex flex-col gap-1.5 text-[13px]">
          {phases.length === 0 && (
            <li className="flex items-center gap-2 text-[var(--ink-soft)]">
              <span className="thinking-pulse" />
              <span>Working…</span>
            </li>
          )}
          {phases.map((p, i) => {
            const isLast = i === phases.length - 1;
            return (
              <li
                key={`${i}-${p}`}
                className={`flex items-center gap-2 ${
                  isLast ? "text-[var(--ink)]" : "text-[var(--ink-soft)]"
                }`}
              >
                {isLast ? (
                  <span className="thinking-pulse" />
                ) : (
                  <Check size={12} className="text-[var(--accent)] shrink-0" />
                )}
                <span>{p}</span>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

function EmptyState({
  onPick: _onPick,
  suggestions: _suggestions,
}: {
  onPick: (q: string) => void;
  suggestions: { q: string; tag: string }[];
}) {
  // Suggestion cards intentionally removed — the empty state now shows only
  // the headline + tagline so the screen feels uncluttered and focuses the
  // user on the composer below.
  void _onPick;
  void _suggestions;
  return (
    // Vertically centered greeting — flex column so the heading sits
    // optically at the visual centre of the chat column rather than
    // hugging the topbar. No description; "Ask Sanhita" prompt copy
    // lives in the composer below and is the call to action.
    <div className="h-full min-h-[60vh] flex items-center justify-center px-4">
      <h2 className="font-display text-3xl sm:text-4xl lg:text-5xl tracking-[-0.025em] text-[var(--ink)] leading-[1.1] text-center max-w-2xl">
        Where would you like to begin, Counsel?
      </h2>
    </div>
  );
}

function ChatBubble({ m, onPickFollowup, onOpenInEditor }: { m: Message; onPickFollowup?: (q: string) => void; onOpenInEditor?: (content: string) => void }) {
  const [copied, setCopied] = useState(false);
  const [savingDoc, setSavingDoc] = useState(false);
  const [savedDoc, setSavedDoc] = useState<string | null>(null);

  if (m.role === "user") {
    return (
      <div className="self-end max-w-[88%] sm:max-w-[80%]">
        <div className="bg-[var(--accent)] text-white rounded-2xl rounded-br-sm px-4 sm:px-5 py-2.5 sm:py-3 leading-relaxed whitespace-pre-wrap break-words">
          {m.content}
        </div>
      </div>
    );
  }

  // Strip markdown for paste-friendly outputs (clipboard, email body).
  const toPlain = (md: string) =>
    md
      .replace(/\*\*(.*?)\*\*/g, "$1")
      .replace(/[*_`]/g, "")
      .replace(/^#+\s*/gm, "");

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(toPlain(m.content));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard API unavailable — silently no-op */
    }
  };

  // Email: open the user's mail client with the answer pre-filled.
  // Lightweight, no OAuth needed — works everywhere.
  const onEmail = () => {
    const subject = "Sanhita research note";
    const body = toPlain(m.content);
    const url = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    window.open(url, "_blank");
  };

  // Save-as-Doc: tries the Google Docs endpoint if Google is connected;
  // otherwise falls back to a local .md download so the user always gets
  // a file out of the click.
  const onSaveDoc = async () => {
    if (savingDoc) return;
    setSavingDoc(true);
    try {
      const res = await fetch("/api/google/docs/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          title: "Sanhita research note",
          body_markdown: m.content,
        }),
      });
      if (res.ok) {
        const data = await res.json();
        if (data?.url) {
          window.open(data.url, "_blank");
          setSavedDoc("Opened in Google Docs");
          setTimeout(() => setSavedDoc(null), 1800);
          return;
        }
      }
      // Fallback — download as .md
      const blob = new Blob([m.content], { type: "text/markdown;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `sanhita-${Date.now()}.md`;
      a.click();
      URL.revokeObjectURL(a.href);
      setSavedDoc("Downloaded");
      setTimeout(() => setSavedDoc(null), 1800);
    } catch {
      // Same fallback path on network error.
      const blob = new Blob([m.content], { type: "text/markdown;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `sanhita-${Date.now()}.md`;
      a.click();
      URL.revokeObjectURL(a.href);
      setSavedDoc("Downloaded");
      setTimeout(() => setSavedDoc(null), 1800);
    } finally {
      setSavingDoc(false);
    }
  };

  // assistant
  return (
    <div className="self-start w-full sm:max-w-[92%] flex flex-col gap-2 min-w-0">
      <div className="text-[10px] tracking-[0.22em] uppercase text-[var(--ink-soft)] flex items-center gap-2 flex-wrap">
        Sanhita
        {m.llm?.provider && m.llm.provider !== "guard" && (
          <span className="bg-[var(--bg-elev)] border border-[var(--line)] px-1.5 py-0.5 rounded text-[9px] tracking-wider">
            via {m.llm.provider}
          </span>
        )}
        {typeof m.validation?.confidence === "number" && (
          <span
            className={`px-1.5 py-0.5 rounded text-[9px] tracking-wider ${
              m.validation.confidence >= 0.85
                ? "bg-[var(--highlight)] text-[var(--accent)]"
                : "bg-[var(--bg-elev)] text-[var(--ink-soft)]"
            }`}
            title={(m.validation.reasons || []).join(" · ")}
          >
            {Math.round(m.validation.confidence * 100)}% grounded
          </span>
        )}
        {m.refused && (
          <span className="bg-[var(--danger)] text-white px-1.5 py-0.5 rounded text-[9px] tracking-wider">
            refused
          </span>
        )}
        {m.mode === "agent" && (
          <span className="bg-[#22C55E]/15 border border-[#22C55E]/40 text-[#22C55E] px-1.5 py-0.5 rounded text-[9px] tracking-wider">
            agent
          </span>
        )}
      </div>
      {m.trace && m.trace.length > 0 && <TraceBreadcrumbs trace={m.trace} />}
      <div
        className="bg-[var(--bg-elev)] border border-[var(--line)] rounded-2xl rounded-bl-sm px-6 py-4 leading-relaxed text-[15px] prose-style"
        dangerouslySetInnerHTML={{ __html: renderMarkdown(m.content) }}
      />

      {/* Web citations — shown when the web search mode returned sources */}
      {m.web_citations && m.web_citations.length > 0 && (
        <div className="mt-2 bg-[var(--bg-elev)] border border-[var(--line)] rounded-xl px-4 py-3">
          <div className="text-[10px] tracking-[0.18em] uppercase text-[var(--ink-soft)] mb-2 flex items-center gap-1.5">
            <span>🌐</span> Web Sources ({m.web_citations.length})
          </div>
          <div className="flex flex-col gap-1.5">
            {m.web_citations.slice(0, 6).map((wc, i) => (
              <a
                key={i}
                href={wc.url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-start gap-2 text-[12px] hover:bg-[var(--bg)] rounded-lg px-2 py-1.5 transition-colors group"
              >
                <span className="text-[var(--accent)] font-mono text-[10px] mt-0.5 shrink-0">[{i + 1}]</span>
                <div className="min-w-0">
                  <div className="text-[var(--ink)] group-hover:text-[var(--accent)] font-medium truncate">{wc.title}</div>
                  <div className="text-[10px] text-[var(--ink-soft)]">
                    {wc.source_name}{wc.date ? ` · ${wc.date}` : ""}
                  </div>
                </div>
              </a>
            ))}
          </div>
        </div>
      )}

      {/* Action row — Copy / Email / Save-as-Doc. Lawyers want the
          answer OUT of the chat: into a draft email, into a shared Doc,
          or simply paste into Word. Hidden for refusals. */}
      {!m.refused && m.content && m.content.length > 40 && (
        <div className="flex items-center gap-4 text-[11px] text-[var(--ink-soft)] pl-1">
          <button
            onClick={onCopy}
            className="flex items-center gap-1.5 hover:text-[var(--ink)] transition-colors"
            title="Copy answer (plain text)"
          >
            {copied ? <Check size={12} /> : <Copy size={12} />}
            <span>{copied ? "Copied" : "Copy"}</span>
          </button>
          <button
            onClick={onEmail}
            className="flex items-center gap-1.5 hover:text-[var(--ink)] transition-colors"
            title="Send as email — opens your mail client with the answer pre-filled"
          >
            <Mail size={12} />
            <span>Email</span>
          </button>
          <button
            onClick={onSaveDoc}
            disabled={savingDoc}
            className="flex items-center gap-1.5 hover:text-[var(--ink)] transition-colors disabled:opacity-60"
            title="Save to Google Docs (or download as Markdown)"
          >
            <FileDown size={12} />
            <span>
              {savingDoc ? "Saving…" : savedDoc ?? "Save as Doc"}
            </span>
          </button>
          {onOpenInEditor && (
            <button
              onClick={() => onOpenInEditor(m.content)}
              className="flex items-center gap-1.5 hover:text-[var(--ink)] transition-colors"
              title="Open in Draft Editor to edit and refine"
            >
              <PenLine size={12} />
              <span>Edit in Draft</span>
            </button>
          )}
        </div>
      )}

      {/* Follow-up suggestion cards — Harvey-style "what to ask next".
          Card grid (2-up on md+) so all three suggestions sit in the
          user's eye-line at once. Click sends the question back through
          the composer's onSend handler. */}
      {m.followups && m.followups.length > 0 && onPickFollowup && (
        <div className="mt-4 flex flex-col gap-2.5">
          <div className="flex items-center gap-1.5 text-[10px] tracking-[0.22em] uppercase text-[var(--ink-soft)] pl-1">
            <Sparkles size={11} className="text-[var(--accent)]" />
            <span>Suggested next steps</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
            {m.followups.map((q, i) => (
              <button
                key={i}
                onClick={() => onPickFollowup(q)}
                className="group relative flex flex-col gap-2 text-left text-[13.5px] leading-snug bg-gradient-to-br from-[var(--bg-elev)] to-[var(--bg)] border border-[var(--line)] hover:border-[var(--accent)] hover:shadow-[0_4px_14px_rgba(120,80,40,0.08)] rounded-2xl px-4 py-3 transition-all duration-150 min-w-0"
              >
                <span className="text-[var(--ink)] min-w-0 break-words pr-5">
                  {q}
                </span>
                <span className="flex items-center justify-between text-[10px] tracking-[0.18em] uppercase text-[var(--ink-soft)] group-hover:text-[var(--accent)] transition-colors">
                  <span>Ask Sanhita</span>
                  <ArrowUpRight
                    size={13}
                    className="shrink-0 transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5"
                  />
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// Harvey-style friendly progress labels. The agent-trace endpoint returns
// raw tool names; users want plain English for what's happening.
const TOOL_ICONS: Record<string, string> = {
  retrieve_cases: "⚖️",
  retrieve_statutes: "📖",
  web_search: "🌐",
  redline_contract: "✂️",
  translate: "🌐",
  vault_search: "📂",
  semantic_search: "🔎",
};

const TOOL_LABELS: Record<string, string> = {
  retrieve_cases: "Searching case law",
  retrieve_statutes: "Pulling statutes",
  web_search: "Researching the open web",
  redline_contract: "Redlining the contract",
  translate: "Translating",
  vault_search: "Searching uploaded documents",
  semantic_search: "Semantic vault retrieval",
};

function _toolLabel(tool?: string): string {
  if (!tool) return "Working";
  return TOOL_LABELS[tool] || tool.replace(/_/g, " ");
}

function TraceBreadcrumbs({ trace }: { trace: TraceStep[] }) {
  return (
    <details className="text-[11px] text-[var(--ink-soft)] bg-[var(--bg-elev)] border border-[var(--line)] rounded-lg px-3 py-2 group">
      <summary className="cursor-pointer flex items-center gap-2 select-none">
        <span className="text-[var(--accent)] font-mono">▸</span>
        <span className="tracking-wider uppercase text-[10px]">
          {trace.length} step{trace.length === 1 ? "" : "s"}
        </span>
        <span className="flex items-center gap-1.5 ml-1 truncate">
          {trace.map((s, i) => (
            <span key={i} className="inline-flex items-center gap-1">
              <span>{TOOL_ICONS[s.tool || ""] || "🔧"}</span>
              <span>{_toolLabel(s.tool)}</span>
              {i < trace.length - 1 && <span className="opacity-40">·</span>}
            </span>
          ))}
        </span>
      </summary>
      <div className="mt-2 space-y-1.5 pl-4 border-l border-[var(--line)]">
        {trace.map((s, i) => (
          <div key={i} className="leading-snug">
            <div className="flex items-center gap-2">
              <span className="text-[var(--accent)]">✓</span>
              <span>{TOOL_ICONS[s.tool || ""] || "🔧"}</span>
              <span className="text-[var(--ink)]">{_toolLabel(s.tool)}</span>
              {typeof s.ms === "number" && (
                <span className="text-[10px] opacity-60">{s.ms}ms</span>
              )}
              {s.error && (
                <span className="text-[10px] text-[var(--danger)]">⚠ {s.error}</span>
              )}
            </div>
            {s.args && Object.keys(s.args).length > 0 && (
              <div className="font-mono text-[10px] opacity-70 break-all pl-6">
                {JSON.stringify(s.args)}
              </div>
            )}
            {s.result_preview && (
              <div className="text-[11px] opacity-80 pl-6 break-words">
                → {s.result_preview}
              </div>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}

// Verdict -> colour mapping for the badge chip
const VERDICT_COLORS: Record<string, string> = {
  allowed:          "bg-[#e6f4ea] text-[#1e8e3e] border-[#a8d5b5]",
  granted:          "bg-[#e6f4ea] text-[#1e8e3e] border-[#a8d5b5]",
  acquitted:        "bg-[#e6f4ea] text-[#1e8e3e] border-[#a8d5b5]",
  dismissed:        "bg-[#fce8e6] text-[#d93025] border-[#f5a9a3]",
  rejected:         "bg-[#fce8e6] text-[#d93025] border-[#f5a9a3]",
  convicted:        "bg-[#fce8e6] text-[#d93025] border-[#f5a9a3]",
  "partly allowed": "bg-[#fef7e0] text-[#b5770d] border-[#fdd87a]",
  "partly dismissed":"bg-[#fef7e0] text-[#b5770d] border-[#fdd87a]",
  disposed:         "bg-[#e8f0fe] text-[#1a73e8] border-[#a8c4f5]",
  quashed:          "bg-[#f3e8fd] text-[#8430ce] border-[#cfabee]",
  stayed:           "bg-[#fef7e0] text-[#b5770d] border-[#fdd87a]",
  remanded:         "bg-[#e8f0fe] text-[#1a73e8] border-[#a8c4f5]",
};

function verdictColor(v?: string): string {
  if (!v) return "bg-[var(--bg-elev)] text-[var(--ink-soft)] border-[var(--line)]";
  const vl = v.toLowerCase();
  for (const [kw, cls] of Object.entries(VERDICT_COLORS)) {
    if (vl.includes(kw)) return cls;
  }
  return "bg-[var(--bg-elev)] text-[var(--ink-soft)] border-[var(--line)]";
}

function SourceCard({ c }: { c: Citation }) {
  const [showPdf, setShowPdf] = useState(false);
  const href = c.pdf_url || c.url || null;
  const tierLabel = c.tier === "SC" ? "Supreme Court" : c.tier === "HC" ? "High Court" : c.tier === "LM" ? "Landmark" : null;
  const tierStyle = c.tier === "SC"
    ? "bg-[#fce8e6] text-[#d93025] border-[#f5a9a3]"
    : c.tier === "LM"
    ? "bg-[#f3e8fd] text-[#8430ce] border-[#cfabee]"
    : "bg-[#e8f0fe] text-[#1a73e8] border-[#a8c4f5]";

  // Document type styling
  const DOC_TYPE_STYLE: Record<string, string> = {
    "Judgment": "bg-[#e8f0fe] text-[#1a73e8]",
    "Legal Document": "bg-[#fef7e0] text-[#b5770d]",
    "Statute": "bg-[#e6f4ea] text-[#1e8e3e]",
    "Legal Q&A": "bg-[#f3e8fd] text-[#8430ce]",
  };
  const docType = c.doc_type || "Judgment";
  const docTypeStyle = DOC_TYPE_STYLE[docType] || DOC_TYPE_STYLE["Judgment"];

  return (
    <div
      data-n={c.n}
      className="source-card group bg-[var(--bg)] border border-[var(--line)] hover:border-[var(--accent)] hover:shadow-[0_2px_12px_rgba(120,80,40,0.10)] rounded-xl p-3 transition-all duration-150 min-w-0 overflow-hidden"
    >
      <div className="flex flex-col gap-2 min-w-0">
        {/* Badge row: citation number + doc type + tier + verdict */}
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-mono text-[10px] font-bold bg-[var(--accent)] text-white rounded px-1.5 py-0.5 shrink-0">
            [{c.n}]
          </span>
          <span className={`text-[9px] font-semibold uppercase tracking-wider rounded px-1.5 py-0.5 shrink-0 ${docTypeStyle}`}>
            {docType}
          </span>
          {tierLabel && (
            <span className={`text-[9px] font-semibold uppercase tracking-wider border rounded px-1.5 py-0.5 shrink-0 ${tierStyle}`}>
              {tierLabel}
            </span>
          )}
          {c.verdict && (
            <span className={`text-[9px] font-semibold uppercase tracking-wider border rounded px-1.5 py-0.5 shrink-0 ${verdictColor(c.verdict)}`}>
              {c.verdict.length > 18 ? c.verdict.slice(0, 18) + "…" : c.verdict}
            </span>
          )}
        </div>

        {/* Title */}
        <span className="font-display text-[13px] leading-snug text-[var(--ink)] line-clamp-2 break-words min-w-0 font-medium">
          {c.title}
        </span>

        {/* Court · Year · Citation */}
        <div className="flex flex-col gap-0.5">
          {(c.court || c.year) && (
            <span className="text-[11px] text-[var(--ink-soft)] break-words leading-snug">
              {[c.court, c.year].filter(Boolean).join(" · ")}
            </span>
          )}
          {c.citation && (
            <span className="font-mono text-[10px] text-[var(--accent)] break-all leading-snug">
              {c.citation}
            </span>
          )}
          {c.judge && (
            <span className="text-[10px] text-[var(--ink-soft)] italic truncate">
              {c.judge}
            </span>
          )}
        </div>

        {/* Excerpt */}
        {c.excerpt && (
          <div className="text-[11px] text-[var(--ink-soft)] italic line-clamp-3 break-words leading-relaxed border-l-2 border-[var(--accent-soft)] pl-2">
            &ldquo;{c.excerpt}&rdquo;
          </div>
        )}

        {/* Action buttons: View PDF inline + Download + Open externally */}
        {href && (
          <div className="flex items-center gap-3 mt-1">
            <button
              onClick={() => setShowPdf(!showPdf)}
              className="flex items-center gap-1 text-[10px] text-[var(--accent)] font-medium hover:underline"
              title="View document inline"
            >
              <Eye size={11} />
              <span>{showPdf ? "Hide" : "View"}</span>
            </button>
            <a
              href={href}
              download
              className="flex items-center gap-1 text-[10px] text-[var(--ink-soft)] hover:text-[var(--accent)] font-medium"
              title="Download PDF"
              onClick={(e) => e.stopPropagation()}
            >
              <Download size={11} />
              <span>Download</span>
            </a>
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 text-[10px] text-[var(--ink-soft)] hover:text-[var(--accent)] font-medium"
              title="Open in new tab"
            >
              <ArrowUpRight size={11} />
              <span>Open</span>
            </a>
          </div>
        )}

        {/* Inline PDF viewer */}
        {showPdf && href && (
          <div className="mt-2 rounded-lg overflow-hidden border border-[var(--line)]" style={{ height: "400px" }}>
            <iframe
              src={href}
              className="w-full h-full"
              title={`PDF: ${c.title}`}
              style={{ border: "none" }}
            />
          </div>
        )}
      </div>
    </div>
  );
}

function safeParse(s: string): Citation[] | undefined {
  try {
    const parsed = JSON.parse(s);
    return Array.isArray(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

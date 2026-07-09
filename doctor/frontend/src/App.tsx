import { useState, useEffect } from "react";
import { Routes, Route, Navigate, NavLink } from "react-router-dom";
import { MessageSquare, BarChart3, Sun, Moon } from "lucide-react";
import DiagnosePage from "@/pages/DiagnosePage";
import EvalPage from "@/pages/EvalPage";
import CasePage from "@/pages/CasePage";
import RunPage from "@/pages/RunPage";

function ThemeToggle() {
  const [dark, setDark] = useState(() => {
    if (typeof window === "undefined") return false;
    return (
      localStorage.getItem("diagdoctor-theme") === "dark" ||
      (!localStorage.getItem("diagdoctor-theme") &&
        window.matchMedia("(prefers-color-scheme: dark)").matches)
    );
  });

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("diagdoctor-theme", dark ? "dark" : "light");
  }, [dark]);

  return (
    <button
      onClick={() => setDark((prev) => !prev)}
      className="inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
      aria-label={dark ? "切换亮色主题" : "切换暗色主题"}
    >
      {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  );
}

const NAV_ITEMS = [
  { to: "/", label: "诊断", icon: MessageSquare },
  { to: "/eval", label: "评测", icon: BarChart3 },
];

function App() {
  return (
    <div className="flex h-screen flex-col bg-background">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-card px-4">
        <span className="mr-4 text-sm font-semibold tracking-tight text-foreground">
          🔬 DiagDoctor
        </span>
        <nav className="flex items-center gap-1">
          {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-muted text-foreground"
                    : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                }`
              }
            >
              <Icon className="size-3.5" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="ml-auto">
          <ThemeToggle />
        </div>
      </header>

      <main className="flex-1 overflow-hidden">
        <Routes>
          <Route path="/" element={<DiagnosePage />} />
          <Route path="/eval" element={<EvalPage />} />
          <Route path="/cases/:id" element={<CasePage />} />
          <Route path="/runs/:name" element={<RunPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default App;

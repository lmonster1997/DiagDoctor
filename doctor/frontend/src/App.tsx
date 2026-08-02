import { useState, useEffect } from "react";
import { Routes, Route, Navigate, NavLink } from "react-router-dom";
import { MessageSquare, Sun, Moon } from "lucide-react";
import DiagnosePage from "@/pages/DiagnosePage";

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
      className="inline-flex size-8 items-center justify-center rounded-md text-[#8a8fa3] hover:bg-white/[0.06] hover:text-[#e4e4ef] transition-all duration-200"
      aria-label={dark ? "切换亮色主题" : "切换暗色主题"}
    >
      {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  );
}

const NAV_ITEMS = [{ to: "/", label: "诊断", icon: MessageSquare }];

function App() {
  return (
    <div className="flex h-screen flex-col bg-[#0f1117]">
      {/* Header - glass bar */}
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-white/[0.06] bg-white/[0.02] px-4 backdrop-blur-xl">
        {/* Logo */}
        <span className="mr-4 flex items-center gap-2 text-sm font-semibold tracking-tight text-[#e4e4ef]">
          <span className="flex size-6 items-center justify-center rounded-md bg-amber-500/15 text-xs">
            🔬
          </span>
          DiagDoctor
        </span>

        {/* Nav pills */}
        <nav className="flex items-center gap-1">
          {NAV_ITEMS.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-all duration-200 ${
                  isActive
                    ? "bg-white/[0.06] text-[#e4e4ef] shadow-sm"
                    : "text-[#8a8fa3] hover:bg-white/[0.03] hover:text-[#e4e4ef]"
                }`
              }
            >
              <Icon className="size-3.5" />
              {label}
            </NavLink>
          ))}
        </nav>

        {/* Right actions */}
        <div className="ml-auto flex items-center gap-2">
          {/* ⌘K hint */}
          <kbd className="hidden rounded border border-white/[0.08] bg-white/[0.03] px-1.5 py-0.5 font-mono text-[10px] text-[#5c6070] sm:inline-block">
            ⌘K
          </kbd>
          <ThemeToggle />
        </div>
      </header>

      <main className="flex-1 overflow-hidden">
        <Routes>
          <Route path="/" element={<DiagnosePage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default App;

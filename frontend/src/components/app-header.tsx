import { Bell, AlertTriangle, Users, UserRound } from "lucide-react";
import { useEffect, useState } from "react";

import { currentUser, summary } from "@/lib/mock-data";

function formatNow(d: Date) {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function AppHeader() {
  const [now, setNow] = useState<string | null>(null);

  useEffect(() => {
    setNow(formatNow(new Date()));
    const t = setInterval(() => setNow(formatNow(new Date())), 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <header className="flex h-16 shrink-0 items-center justify-between border-b border-border bg-card px-6">
      <div className="flex items-center gap-6">
        <div className="tabular text-sm font-semibold text-foreground">{now ?? "--:--:--"}</div>
        <div className="flex items-center gap-2 rounded-md bg-secondary px-3 py-1.5 text-sm">
          <Users className="size-4 text-primary" />
          <span className="text-muted-foreground">현재 환자</span>
          <span className="tabular font-bold text-foreground">{summary.total}명</span>
        </div>
        <div className="flex items-center gap-2 rounded-md bg-risk-critical-soft px-3 py-1.5 text-sm">
          <AlertTriangle className="size-4 text-risk-critical" />
          <span className="text-risk-critical/80">위험 환자</span>
          <span className="tabular font-bold text-risk-critical">
            {summary.critical + summary.rising}명
          </span>
        </div>
      </div>

      <div className="flex items-center gap-4">
        <button
          type="button"
          className="relative rounded-md p-2 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          aria-label="알림"
        >
          <Bell className="size-5" />
          <span className="absolute right-1.5 top-1.5 flex size-4 items-center justify-center rounded-full bg-risk-critical text-[10px] font-bold text-primary-foreground">
            {summary.aiAlertsToday}
          </span>
        </button>
        <div className="flex items-center gap-2 border-l border-border pl-4">
          <div className="flex size-8 items-center justify-center rounded-full bg-navy text-navy-foreground">
            <UserRound className="size-4" />
          </div>
          <div className="text-sm leading-tight">
            <p className="font-semibold text-foreground">
              {currentUser.dept} {currentUser.name}
            </p>
            <p className="text-xs text-muted-foreground">{currentUser.role}</p>
          </div>
        </div>
      </div>
    </header>
  );
}

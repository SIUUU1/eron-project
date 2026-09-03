import { Link, useRouterState } from "@tanstack/react-router";
import { Activity, ClipboardCheck, LayoutDashboard, Settings } from "lucide-react";

const items = [
  { title: "응급실 현황", url: "/", icon: LayoutDashboard },
  { title: "환자 모니터링", url: "/monitoring", icon: Activity },
  { title: "AI 진료기록 및 누락 검사", url: "/records", icon: ClipboardCheck },
  { title: "시스템 설정", url: "/settings", icon: Settings },
];

export function AppSidebar() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });

  const isActive = (url: string) => (url === "/" ? pathname === "/" : pathname.startsWith(url));

  return (
    <aside className="flex w-64 shrink-0 flex-col bg-sidebar text-sidebar-foreground">
      <div className="flex items-center justify-center border-b border-sidebar-border px-5 py-4">
        {/* 로고를 누르면 홈(응급실 현황)으로 돌아간다 */}
        <Link
          to="/"
          aria-label="ER:ON 응급실 현황으로 이동"
          className="rounded-md transition-opacity hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring"
        >
          <img
            src="/eron-logo.png"
            alt="predict Record ER:ON"
            width={440}
            height={321}
            className="h-20 w-auto max-w-full object-contain"
          />
        </Link>
      </div>

      <nav className="flex flex-1 flex-col gap-1 p-3">
        {items.map((item) => (
          <Link
            key={item.url}
            to={item.url}
            className={`flex items-center gap-3 rounded-md px-3 py-2.5 text-sm transition-colors ${
              isActive(item.url)
                ? "bg-sidebar-accent font-semibold text-sidebar-accent-foreground"
                : "text-sidebar-foreground/75 hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground"
            }`}
          >
            <item.icon className="size-4 shrink-0" />
            <span className="leading-tight">{item.title}</span>
          </Link>
        ))}
      </nav>

      <div className="border-t border-sidebar-border px-5 py-4 text-[11px] leading-relaxed text-sidebar-foreground/55">
        AI 기반 응급실 환자안전·의무기록
        <br />
        품질관리 시스템 · Demo v0.9
      </div>
    </aside>
  );
}

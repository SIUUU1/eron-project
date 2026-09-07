import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  Outlet,
  Link,
  createRootRouteWithContext,
  useRouter,
  useRouterState,
  HeadContent,
  Scripts,
} from "@tanstack/react-router";
import { Info } from "lucide-react";
import { useEffect, type ReactNode } from "react";

import { AppHeader } from "@/components/app-header";
import { AppSidebar } from "@/components/app-sidebar";
import { Toaster } from "@/components/ui/sonner";

import appCss from "../styles.css?url";
import { reportLovableError } from "../lib/lovable-error-reporting";

function NotFoundComponent() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center px-4">
      <div className="max-w-md text-center">
        <h1 className="text-7xl font-bold text-foreground">404</h1>
        <h2 className="mt-4 text-xl font-semibold text-foreground">페이지를 찾을 수 없습니다</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          요청한 화면이 존재하지 않거나 이동되었습니다.
        </p>
        <div className="mt-6">
          <Link
            to="/"
            className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            응급실 현황으로 이동
          </Link>
        </div>
      </div>
    </div>
  );
}

function ErrorComponent({ error, reset }: { error: Error; reset: () => void }) {
  console.error(error);
  const router = useRouter();
  useEffect(() => {
    reportLovableError(error, { boundary: "tanstack_root_error_component" });
  }, [error]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center px-4">
      <div className="max-w-md text-center">
        <h1 className="text-xl font-semibold tracking-tight text-foreground">
          화면을 불러오지 못했습니다
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          잠시 후 다시 시도하거나 응급실 현황으로 이동해 주세요.
        </p>
        <div className="mt-6 flex flex-wrap justify-center gap-2">
          <button
            onClick={() => {
              router.invalidate();
              reset();
            }}
            className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
          >
            다시 시도
          </button>
          <a
            href="/"
            className="inline-flex items-center justify-center rounded-md border border-input bg-background px-4 py-2 text-sm font-medium text-foreground"
          >
            응급실 현황
          </a>
        </div>
      </div>
    </div>
  );
}

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: "ER:ON(이로운)" },
      {
        name: "description",
        content: "AI 기반 응급실 환자안전·의무기록 품질관리 시스템 시연 프로토타입입니다.",
      },
      { property: "og:title", content: "ER:ON(이로운)" },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
    links: [
      { rel: "stylesheet", href: appCss },
      { rel: "preconnect", href: "https://fonts.googleapis.com" },
      { rel: "preconnect", href: "https://fonts.gstatic.com", crossOrigin: "anonymous" },
      {
        rel: "stylesheet",
        href: "https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;900&family=IBM+Plex+Mono:wght@400;600&display=swap",
      },
      { rel: "icon", href: "/favicon.ico", sizes: "16x16 32x32 48x48", type: "image/x-icon" },
      { rel: "apple-touch-icon", href: "/apple-touch-icon.png", sizes: "180x180" },
    ],
  }),
  shellComponent: RootShell,
  component: RootComponent,
  notFoundComponent: NotFoundComponent,
  errorComponent: ErrorComponent,
});

function RootShell({ children }: { children: ReactNode }) {
  return (
    <html lang="ko">
      <head>
        <HeadContent />
      </head>
      <body>
        {children}
        <Scripts />
      </body>
    </html>
  );
}

function RootComponent() {
  const { queryClient } = Route.useRouteContext();
  // ?mobile=1 강제 진입점(예: /records?mobile=1): 사이드바·헤더 없이
  // 녹음/기록 화면만 단독으로 보여준다. 휴대폰에서 곧장 녹음창으로 쓰기 위한 용도.
  const isMobileCompact = useRouterState({
    select: (s) =>
      String((s.location.search as Record<string, unknown> | undefined)?.mobile) === "1",
  });

  if (isMobileCompact) {
    return (
      <QueryClientProvider client={queryClient}>
        <div className="flex min-h-screen w-full flex-col bg-background">
          <div className="flex items-center justify-center gap-2 bg-navy px-4 py-2 text-center text-xs text-navy-foreground/85">
            <Info className="size-3.5 shrink-0" />본 시스템은 프로젝트 시연용이며 실제 의료 판단을
            대신하지 않습니다.
          </div>
          <main className="min-w-0 flex-1 p-4">
            {/* Required: nested routes render here. */}
            <Outlet />
          </main>
        </div>
        <Toaster position="top-right" />
      </QueryClientProvider>
    );
  }

  return (
    <QueryClientProvider client={queryClient}>
      <div className="flex min-h-screen w-full bg-background">
        <AppSidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex items-center justify-center gap-2 bg-navy px-6 py-2 text-xs text-navy-foreground/85">
            <Info className="size-3.5" />본 시스템은 프로젝트 시연용이며 실제 의료 판단을 대신하지
            않습니다.
          </div>
          <AppHeader />
          <main className="min-w-0 flex-1 p-6">
            {/* Required: nested routes render here. */}
            <Outlet />
          </main>
        </div>
      </div>
      <Toaster position="top-right" />
    </QueryClientProvider>
  );
}

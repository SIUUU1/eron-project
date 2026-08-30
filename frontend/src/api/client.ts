/**
 * 백엔드 호출 래퍼.
 *
 * nginx 가 프론트와 API 를 동일 오리진으로 노출하므로 기본은 상대 경로다.
 * 컨테이너 밖에서 vite dev 를 띄울 때만 VITE_API_BASE_URL 을 채운다.
 * (VITE_* 는 빌드 시점에 번들에 고정되므로 절대 URL 을 기본값으로 두지 않는다.)
 */

const BASE = import.meta.env?.["VITE_API_BASE_URL"] ?? "";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    // exactOptionalPropertyTypes 때문에 undefined 를 그대로 넘길 수 없다
    res = await fetch(`${BASE}${path}`, {
      signal: signal ?? null,
      headers: { Accept: "application/json" },
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(0, "서버에 연결할 수 없습니다.");
  }

  if (!res.ok) {
    // nginx 가 만드는 502/504 는 JSON 이 아니라 HTML 이다.
    // res.json() 파싱 실패를 반드시 처리해야 한다.
    const detail = await res
      .json()
      .then((body: { detail?: unknown }) =>
        typeof body.detail === "string" ? body.detail : res.statusText,
      )
      .catch(() => res.statusText || "요청을 처리하지 못했습니다.");
    throw new ApiError(res.status, detail);
  }

  return (await res.json()) as T;
}

export async function apiPost<TRequest, TResponse>(
  path: string,
  body: TRequest,
  signal?: AbortSignal,
): Promise<TResponse> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method: "POST",
      signal: signal ?? null,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(0, "서버에 연결할 수 없습니다.");
  }

  if (!res.ok) {
    const message = await res
      .json()
      .then((responseBody: { detail?: unknown; error?: unknown }) => {
        if (typeof responseBody.detail === "string") return responseBody.detail;
        if (typeof responseBody.error === "string") return responseBody.error;
        return res.statusText;
      })
      .catch(() => res.statusText || "요청을 처리하지 못했습니다.");
    throw new ApiError(res.status, message);
  }

  return (await res.json()) as TResponse;
}

export async function apiPostFormData<TResponse>(
  path: string,
  body: FormData,
  signal?: AbortSignal,
): Promise<TResponse> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method: "POST",
      signal: signal ?? null,
      headers: { Accept: "application/json" },
      body,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(0, "서버에 연결할 수 없습니다.");
  }

  if (!res.ok) {
    const message = await res
      .json()
      .then((responseBody: { detail?: unknown; error?: unknown }) => {
        if (typeof responseBody.detail === "string") return responseBody.detail;
        if (typeof responseBody.error === "string") return responseBody.error;
        return res.statusText;
      })
      .catch(() => res.statusText || "요청을 처리하지 못했습니다.");
    throw new ApiError(res.status, message);
  }

  return (await res.json()) as TResponse;
}

export function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

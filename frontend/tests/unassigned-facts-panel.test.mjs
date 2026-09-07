import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(
  new URL("../src/components/records/unassigned-facts-panel.tsx", import.meta.url),
  "utf8",
);
const compiled = ts
  .transpileModule(source, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext },
  })
  .outputText.replaceAll(
    "react/jsx-runtime",
    pathToFileURL(require.resolve("react/jsx-runtime")).href,
  );
const { UnassignedFactsPanel } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

test("미배정 정보가 없으면 별도 목록을 표시하지 않는다", () => {
  assert.equal(renderToStaticMarkup(createElement(UnassignedFactsPanel, { facts: [] })), "");
});

test("부인 상태, 원문, 번역, 시작·종료초를 표시하고 임상 필드 입력은 만들지 않는다", () => {
  const html = renderToStaticMarkup(
    createElement(UnassignedFactsPanel, {
      facts: [
        {
          fact_id: "f1",
          candidate_label: "Contrast media allergy",
          fact: { type: "MATCHED_TERM", assertion: "DENIED" },
          evidence: [
            {
              segment_id: "s1",
              start: 2.26,
              end: 3.94,
              raw_text: "조영제 알레르기 없어요",
              translated_text_en: "No contrast allergy",
            },
          ],
        },
      ],
    }),
  );
  for (const expected of [
    "초안 미반영 정보",
    "명시적으로 부인함",
    "Contrast media allergy",
    "조영제 알레르기 없어요",
    "No contrast allergy",
    "2.26–3.94초",
    "자동 추가되지 않습니다",
  ])
    assert.ok(html.includes(expected), expected);
  assert.doesNotMatch(html, /<(input|textarea|button)\b/);
});

test("불확실한 Fact와 누락된 근거는 확정하거나 만들어 내지 않는다", () => {
  const html = renderToStaticMarkup(
    createElement(UnassignedFactsPanel, {
      facts: [
        {
          fact_id: "f2",
          fact: { text: "Father <unknown>", assertion: "UNCERTAIN", values: { value: 8 } },
          evidence: [],
        },
      ],
    }),
  );
  assert.ok(html.includes("불확실함"));
  assert.ok(html.includes("연결된 대화 근거를 확인할 수 없습니다"));
  assert.ok(html.includes("Father &lt;unknown&gt;"));
  assert.ok(html.includes("8"));
});

import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { ProviderUsage } from "./ProvidersPanel";

it.each([0, 2500])("shows the local daily quota accurately (%s)", (limit) => {
  const html = renderToStaticMarkup(<ProviderUsage calls={2501} limit={limit} />);
  expect(html).toContain("2501");
  expect(html).toContain(limit ? "/ 2500" : "суточная квота Фонотеки отключена");
  expect(html).not.toContain("/ 0 ");
});

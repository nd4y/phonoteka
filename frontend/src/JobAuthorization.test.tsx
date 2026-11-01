import { Children, isValidElement } from "react";
import type { ReactElement, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import {
  JobAuthorizationForm,
  JobAuthorizationPanel,
  reauthorizationMessage,
  reauthorizationRequest,
} from "./JobAuthorization";
import { JobsView } from "./JobsView";
import type { Job } from "./types";

const job: Job = {
  id: "same-job",
  kind: "apply",
  state: "waiting_authorization",
  stage: "waiting_authorization",
  total: 10,
  completed: 4,
  failed: 1,
  progress: 50,
  cancel_requested: false,
  created_at: "",
  updated_at: "",
  authorization: {
    reason: "authorization_revoked",
    version: 3,
    required_scopes: ["jobs:create", "files:write"],
    allowed_scopes: ["jobs:create", "files:write"],
    reauthorize_allowed: true,
  },
};
function elements(node: ReactNode): ReactElement<Record<string, unknown>>[] {
  if (!isValidElement<Record<string, unknown>>(node)) return [];
  return [node, ...Children.toArray(node.props.children as ReactNode).flatMap(elements)];
}
const form = (scopes: string[] = [], current = job, pending = false) =>
  JobAuthorizationForm({ job: current, scopes, pending, onScopes: vi.fn(), onGrant: vi.fn() });

describe("explicit owner reauthorization of the remaining same-job operations", () => {
  it("shows a distinct waiting card with cancellation and no ordinary resume or provider retry promise", () => {
    const markup = renderToStaticMarkup(
      <JobsView
        jobs={[{ ...job, pause_requested: true, retry_at: 123 }]}
        refresh={async () => {}}
        onCancel={async () => {}}
      />,
    );
    expect(markup).toContain("job-card--authorization");
    expect(markup).toContain("Нужно разрешение");
    expect(markup).toContain("ETA после подтверждения");
    expect(markup).toContain("Отменить");
    expect(markup).not.toContain(">Продолжить<");
    expect(markup).not.toContain(">Пауза<");
    expect(markup).not.toContain("Автоматический повтор после");
  });

  it("starts without selected rights and explains the safe retained-results boundary", () => {
    const markup = renderToStaticMarkup(
      <JobAuthorizationPanel job={job} pending={false} onGrant={() => {}} />,
    );
    expect(markup).toContain("Разрешение, выданное этой задаче, отозвано.");
    expect(markup).toContain("Идентификатор задачи и выполненные попытки сохраняются");
    expect(markup).toContain("Запись и откат изменений в рабочей коллекции");
    expect(markup).not.toContain('checked=""');
    expect(markup).not.toContain("Публикация и экспорт");
    expect(elements(form()).find((node) => node.type === "button")!.props.disabled).toBe(true);
  });

  it("sends only explicitly selected exact required scopes and the current authorization version", () => {
    expect(reauthorizationRequest(job, ["files:write", "jobs:create"])).toEqual({
      method: "POST",
      body: JSON.stringify({ scopes: ["jobs:create", "files:write"], authorization_version: 3 }),
    });
    for (const scopes of [
      [],
      ["jobs:create"],
      ["jobs:create", "files:write", "publish:write"],
      ["jobs:create", "files:write", "files:write"],
    ]) {
      expect(reauthorizationRequest(job, scopes)).toBeNull();
    }
    const publish = {
      ...job,
      kind: "publish",
      authorization: {
        ...job.authorization!,
        required_scopes: ["jobs:create", "publish:write"],
        allowed_scopes: ["jobs:create", "publish:write"],
      },
    };
    expect(reauthorizationRequest(publish, ["jobs:create", "files:write"])).toBeNull();
    expect(
      JSON.parse(reauthorizationRequest(publish, ["jobs:create", "publish:write"])!.body).scopes,
    ).toEqual(["jobs:create", "publish:write"]);
  });

  it("uses actual checkbox and submit callbacks without granting on a partial selection", () => {
    const onScopes = vi.fn(),
      onGrant = vi.fn(),
      preventDefault = vi.fn();
    const partial = JobAuthorizationForm({
      job,
      scopes: ["jobs:create"],
      pending: false,
      onScopes,
      onGrant,
    });
    const checkbox = elements(partial).filter((node) => node.type === "input")[1];
    (checkbox.props.onChange as (event: { target: { checked: boolean } }) => void)({
      target: { checked: true },
    });
    expect(onScopes).toHaveBeenCalledExactlyOnceWith(["jobs:create", "files:write"]);
    (partial.props.onSubmit as (event: { preventDefault: () => void }) => void)({ preventDefault });
    expect(onGrant).not.toHaveBeenCalled();
    const confirmed = JobAuthorizationForm({
      job,
      scopes: onScopes.mock.calls[0][0],
      pending: false,
      onScopes,
      onGrant,
    });
    (confirmed.props.onSubmit as (event: { preventDefault: () => void }) => void)({
      preventDefault,
    });
    expect(onGrant).toHaveBeenCalledExactlyOnceWith(["jobs:create", "files:write"]);
    expect(preventDefault).toHaveBeenCalledTimes(2);
  });

  it("waits for active operations to drain and refuses pending, cancelled and terminal grants", () => {
    const draining = {
      ...job,
      state: "running",
      authorization: { ...job.authorization!, reauthorize_allowed: false },
    };
    const markup = renderToStaticMarkup(form(["jobs:create", "files:write"], draining));
    expect(markup).toContain("Начатые операции завершаются безопасно");
    expect(
      elements(form(["jobs:create", "files:write"], draining)).find(
        (node) => node.type === "fieldset",
      )!.props.disabled,
    ).toBe(true);
    expect(reauthorizationRequest(draining, ["jobs:create", "files:write"])).toBeNull();
    expect(
      elements(form(["jobs:create", "files:write"], job, true)).find(
        (node) => node.type === "button",
      )!.props.disabled,
    ).toBe(true);
    for (const current of [
      { ...job, cancel_requested: true },
      { ...job, state: "cancelled" },
      { ...job, state: "completed" },
    ]) {
      expect(reauthorizationRequest(current, ["jobs:create", "files:write"])).toBeNull();
    }
  });

  it("fails closed on malformed or broader scope contracts without rendering an injected reason", () => {
    for (const authorization of [
      undefined,
      { ...job.authorization!, version: -1 },
      { ...job.authorization!, required_scopes: null } as unknown as Job["authorization"],
      { ...job.authorization!, reauthorize_allowed: "false" } as unknown as Job["authorization"],
      { ...job.authorization!, allowed_scopes: ["jobs:create", "files:write", "publish:write"] },
      {
        ...job.authorization!,
        required_scopes: ["jobs:create", "admin:all"],
        allowed_scopes: ["jobs:create", "admin:all"],
      },
    ]) {
      const current = { ...job, authorization };
      expect(reauthorizationRequest(current, ["jobs:create", "files:write"])).toBeNull();
      expect(renderToStaticMarkup(form([], current))).toContain("Обновите очередь");
    }
    const markup = renderToStaticMarkup(
      form([], {
        ...job,
        authorization: { ...job.authorization!, reason: "private/raw/path/or/message" },
      }),
    );
    expect(markup).not.toContain("private/raw");
    expect(markup).toContain("актуальное разрешение");
  });

  it("explains a stale authorization version without treating it as a file revision conflict", () => {
    expect(
      reauthorizationMessage(new ApiError("raw code", 409, "authorization_version_conflict")),
    ).toContain("Условия подтверждения изменились");
    expect(
      reauthorizationMessage(new ApiError("raw code", 409, "authorization_required")),
    ).toContain("подтвердите оставшиеся операции");
    expect(reauthorizationMessage(new ApiError("raw code", 422, "invalid_scopes"))).toContain(
      "Разрешения не совпадают",
    );
  });
});

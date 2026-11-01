import { Children, isValidElement } from "react";
import type { ReactElement, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import {
  classificationRequest,
  recordingLabel,
  recordingPanelKey,
  RecordingTypeForm,
  RecordingTypePanel,
} from "./RecordingTypePanel";
import type { Track } from "./types";

const track = {
  id: "fixture-001",
  sha256: "a".repeat(64),
  format: "flac",
  tags: {},
  relative_path: "fixture.flac",
  status: "needs_review",
  revision: "1",
  has_art: false,
  can_write: false,
} satisfies Track;
function elements(node: ReactNode): ReactElement<Record<string, unknown>>[] {
  if (!isValidElement<Record<string, unknown>>(node)) return [];
  return [node, ...Children.toArray(node.props.children as ReactNode).flatMap(elements)];
}

describe("explicit recording classification", () => {
  it("remounts only the recording decision draft when new same-revision segment evidence arrives", () => {
    const conflicting: Track = {
      ...track,
      possible_mix: true,
      record_type: "possible_mix",
      recording_classification: {
        kind: "possible_mix",
        origin: "segment_conflict",
        input_sha256: track.sha256,
        version: 0,
        reason: "conflicting_segment_recording_ids",
      },
    };
    expect(recordingPanelKey(conflicting)).not.toBe(recordingPanelKey(track));
    expect(
      recordingPanelKey({
        ...conflicting,
        tags: { title: "Draft still belongs to the metadata editor" },
      }),
    ).toBe(recordingPanelKey(conflicting));
    expect(
      recordingPanelKey({
        ...conflicting,
        recording_classification: {
          ...conflicting.recording_classification!,
          origin: "owner",
          version: 1,
        },
      }),
    ).not.toBe(recordingPanelKey(conflicting));
  });

  it("shows a composite recording warning without claiming automatic splitting or accepted metadata", () => {
    const mix: Track = {
      ...track,
      possible_mix: true,
      record_type: "possible_mix",
      recording_classification: {
        kind: "possible_mix",
        origin: "segment_conflict",
        input_sha256: track.sha256,
        version: 0,
        reason: "conflicting_segment_recording_ids",
      },
    };
    expect(recordingLabel(mix)).toBe("Возможный микс");
    const markup = renderToStaticMarkup(
      <RecordingTypePanel
        track={mix}
        disabled={false}
        onChanged={() => {}}
        onDirtyChange={() => {}}
      />,
    );
    expect(markup).toContain("Разные фрагменты аудио сопоставлены с разными записями");
    expect(markup).toContain("не принимает теги, текст или таймкоды и не разделяет аудио");
    expect(markup).toContain("до подтверждения");
    expect(markup).not.toContain("conflicting_segment_recording_ids");
  });

  it("binds the owner decision to exact current SHA and decision version", () => {
    const current: Track = {
      ...track,
      recording_classification: {
        kind: "possible_mix",
        origin: "owner",
        input_sha256: track.sha256,
        version: 3,
        reason: "checked both sections",
      },
    };
    const request = classificationRequest(
      current,
      "single_recording",
      "  Проверено по всему аудио  ",
    );
    expect(request.method).toBe("PUT");
    expect(request.headers).toEqual({ "If-Match": track.sha256 });
    expect(JSON.parse(request.body)).toEqual({
      kind: "single_recording",
      base_version: 3,
      reason: "Проверено по всему аудио",
    });
    const stale = { ...current, sha256: "b".repeat(64) };
    expect(
      JSON.parse(classificationRequest(stale, "possible_mix", "Причина решения").body).base_version,
    ).toBe(0);
  });

  it("collects a bounded reason and an explicit selected type without accepting other fields", () => {
    const onKind = vi.fn(),
      onReason = vi.fn(),
      onSave = vi.fn(),
      preventDefault = vi.fn();
    const tree = RecordingTypeForm({
      kind: "possible_mix",
      reason: "Проверены фрагменты",
      disabled: false,
      saving: false,
      onKind,
      onReason,
      onSave,
    });
    const nodes = elements(tree),
      select = nodes.find((node) => node.type === "select")!,
      textarea = nodes.find((node) => node.type === "textarea")!;
    (select.props.onChange as (event: { target: { value: string } }) => void)({
      target: { value: "single_recording" },
    });
    (textarea.props.onChange as (event: { target: { value: string } }) => void)({
      target: { value: "Проверена запись целиком" },
    });
    (tree.props.onSubmit as (event: { preventDefault: () => void }) => void)({ preventDefault });
    expect(onKind.mock.calls).toEqual([["single_recording"]]);
    expect(onReason.mock.calls).toEqual([["Проверена запись целиком"]]);
    expect(onSave).toHaveBeenCalledOnce();
    expect(preventDefault).toHaveBeenCalledOnce();
    expect(textarea.props.maxLength).toBe(1000);
    expect(textarea.props.minLength).toBe(5);
  });

  it("keeps the decision disabled until a nonempty meaningful reason is entered", () => {
    for (const reason of ["", "    ", "four"]) {
      const nodes = elements(
        RecordingTypeForm({
          kind: "single_recording",
          reason,
          disabled: false,
          saving: false,
          onKind: () => {},
          onReason: () => {},
          onSave: () => {},
        }),
      );
      expect(nodes.find((node) => node.type === "button")!.props.disabled).toBe(true);
    }
  });

  it("displays revision-bound owner decisions and preserves the CUE classification boundary", () => {
    const decided: Track = {
      ...track,
      record_type: "single_recording",
      recording_classification: {
        kind: "single_recording",
        origin: "owner",
        input_sha256: track.sha256,
        version: 2,
        reason: "Проверено по аудио",
      },
    };
    expect(recordingLabel(decided)).toBe("Отдельная запись");
    expect(
      renderToStaticMarkup(
        <RecordingTypePanel
          track={decided}
          disabled={false}
          onChanged={() => {}}
          onDirtyChange={() => {}}
        />,
      ),
    ).toContain("Подтверждено вручную, версия 2: Проверено по аудио");
    for (const cue of [
      { ...track, format: "cue" },
      { ...track, cue_album_image: true },
    ]) {
      const markup = renderToStaticMarkup(
        <RecordingTypePanel
          track={cue}
          disabled={false}
          onChanged={() => {}}
          onDirtyChange={() => {}}
        />,
      );
      expect(markup).toContain("определяется документом CUE");
      expect(markup).not.toContain("<form");
    }
  });
});

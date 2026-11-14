"use client";

/**
 * A message's files (TH-03 .. TH-07): drawn by kind and state; bytes fetched with the session's
 * client as a Blob only when needed (an image ≤ 2 MiB in view, audio or video on first play, a
 * document on Download), at most three at a time, checked against the tile's kind, and shown
 * through an object URL the registry revokes when this tile lets go.
 */
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { ApiError } from "@wasla/api-client";
import { MediaTile, TranscriptBlock, type MediaTileState } from "@wasla/ui";

import { AUTO_IMAGE_BYTES, blobFits, safeFilename } from "../../lib/inbox/media";
import { useSession } from "../session-provider";
import { useInboxRuntime } from "./inbox-runtime";
import type { MediaRead } from "./people";

type Kind = "image" | "audio" | "video" | "document";

function kindOf(media: MediaRead): Kind {
  const kind = media.kind as string;
  return kind === "image" || kind === "audio" || kind === "video" ? kind : "document";
}

/** Fetch one file's bytes with the session's client (TH-04). */
async function fetchBlob(
  api: ReturnType<typeof useSession>["session"]["api"],
  conversationId: string,
  mediaId: string,
  signal: AbortSignal,
): Promise<Blob> {
  const result = await api.GET("/api/v1/conversations/{conversation_id}/media/{media_id}", {
    params: { path: { conversation_id: conversationId, media_id: mediaId } },
    parseAs: "blob",
    signal,
  });
  if (!result.response.ok) {
    throw new ApiError({
      status: result.response.status,
      code: result.response.status === 404 ? "not_found" : "unexpected_response",
    });
  }
  return result.data as Blob;
}

export function MessageMedia({
  conversationId,
  media,
  from,
  onOpenImage,
  registerImage,
}: {
  conversationId: string;
  media: MediaRead;
  from: string;
  onOpenImage: (mediaId: string) => void;
  registerImage?: (mediaId: string, src: string | null) => void;
}) {
  const t = useTranslations("app.inbox.media");
  const format = useFormatter();
  const { session } = useSession();
  const runtime = useInboxRuntime();
  const owner = useId();
  const kind = kindOf(media);
  const [src, setSrc] = useState<string | null>(() =>
    runtime.media.has(media.id) ? runtime.media.acquire(media.id, owner) : null,
  );
  const [state, setState] = useState<MediaTileState>(() => {
    if (media.status !== "available") return media.status;
    if (src) return "ready";
    if (kind === "image") return (media.byte_size ?? 0) <= AUTO_IMAGE_BYTES ? "loading" : "ask";
    return "ask";
  });
  const abort = useRef<AbortController | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(
    async (thenDownload = false) => {
      if (media.status !== "available") return;
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      setState("loading");
      try {
        const blob = await runtime.gate.run(() =>
          fetchBlob(session.api, conversationId, media.id, controller.signal),
        );
        if (!blobFits(kind, blob.type || media.mime_type || "")) {
          setState("unsupported");
          return;
        }
        const url = runtime.media.acquire(media.id, owner, blob);
        if (!url) return;
        if (thenDownload || kind === "document") {
          const anchor = document.createElement("a");
          anchor.href = url;
          anchor.download = safeFilename(media.filename, t("defaultFilename"));
          document.body.append(anchor);
          anchor.click();
          anchor.remove();
          if (kind === "document") {
            runtime.media.release(media.id, owner);
            setState("ask");
            return;
          }
        }
        setSrc(url);
        setState("ready");
        if (kind === "image") registerImage?.(media.id, url);
      } catch (error) {
        if (controller.signal.aborted) return;
        setState(
          error instanceof ApiError && error.code === "not_found" ? "unavailable" : "failed",
        );
      }
    },
    [media, kind, runtime, owner, session.api, conversationId, registerImage, t],
  );

  // An image of at most 2 MiB loads when its tile comes into view (TH-04).
  useEffect(() => {
    if (state !== "loading" || kind !== "image" || src) return;
    const node = boxRef.current;
    if (!node || typeof IntersectionObserver === "undefined") {
      void load();
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        void load();
      }
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [state, kind, src, load]);

  // Let go of the URL (and cancel a fetch) when the tile goes (TH-04).
  useEffect(
    () => () => {
      abort.current?.abort();
      runtime.media.release(media.id, owner);
      registerImage?.(media.id, null);
    },
    [runtime, media.id, owner, registerImage],
  );

  const bytes = media.byte_size ?? 0;
  const size = format.number(bytes / 1024 < 1024 ? bytes / 1024 : bytes / 1048576, {
    maximumFractionDigits: 1,
    numberingSystem: "latn",
  });
  const sizeLabel = `${size} ${bytes / 1024 < 1024 ? t("kb") : t("mb")}`;
  const label =
    state === "pending"
      ? t("pending")
      : state === "failed"
        ? t("failed")
        : state === "unavailable"
          ? t("unavailable")
          : state === "unsupported"
            ? t("unsupported")
            : media.is_voice
              ? t("voice")
              : t(`kind.${kind}`);
  const caption = media.caption ? (
    <p dir="auto" className="m-0 whitespace-pre-wrap text-sm text-text">
      {media.caption}
    </p>
  ) : null;
  const transcriptState = media.transcript_status;
  const transcript =
    kind === "audio" ? (
      <TranscriptBlock
        label={t("transcript")}
        state={transcriptState}
        text={media.transcript}
        stateLabel={transcriptState === "pending" ? t("transcribing") : t("noTranscript")}
        showLabel={t("showTranscript")}
        hideLabel={t("hideTranscript")}
        long={
          (media.transcript ?? "").split("\n").length > 3 || (media.transcript ?? "").length > 240
        }
      />
    ) : null;

  const tileState: MediaTileState = state === "unsupported" ? "ask" : state;
  return (
    <div ref={boxRef} className="flex flex-col gap-2" data-media-id={media.id}>
      <MediaTile
        kind={state === "unsupported" ? "document" : kind}
        state={tileState}
        src={src}
        alt={media.caption || t("imageFrom", { name: from })}
        label={label}
        filename={kind === "document" ? safeFilename(media.filename, t("defaultFilename")) : null}
        size={sizeLabel}
        actionLabel={
          kind === "document" || state === "unsupported"
            ? t("download")
            : kind === "image"
              ? t("loadImage")
              : media.is_voice
                ? t("playVoice")
                : kind === "audio"
                  ? t("playAudio")
                  : t("playVideo")
        }
        onAction={() => void load(kind === "document" || state === "unsupported")}
        onOpen={() => onOpenImage(media.id)}
        openLabel={t("open")}
        caption={caption}
      />
      {transcript}
    </div>
  );
}

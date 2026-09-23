"use client";

import { Check, ExternalLink, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  type CompanyEntity,
  decideRelationshipSuggestion,
  deleteRelationship,
  type EntityRelationship,
  listEntities,
  listEntityRelationships,
  listRelationshipSuggestions,
  type PreflightEntity,
  type RelationshipKind,
  type RelationshipSuggestion,
} from "@/lib/api";
import { formatTicker } from "@/lib/utils";

const KIND_LABEL: Record<RelationshipKind, string> = {
  parent_of: "parent of",
  joint_venture: "JV partner",
  major_shareholder: "shareholder of",
};

const BUCKET_KINDS: {
  bucket: "parents" | "joint_venture_partners" | "major_shareholders";
  label: string;
}[] = [
  { bucket: "parents", label: "Parents" },
  { bucket: "joint_venture_partners", label: "JV partners" },
  { bucket: "major_shareholders", label: "Major shareholders" },
];

export default function RelationshipsPage() {
  const [entities, setEntities] = useState<CompanyEntity[]>([]);
  const [relationships, setRelationships] = useState<EntityRelationship[]>([]);
  const [suggestions, setSuggestions] = useState<RelationshipSuggestion[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [es, rs, ss] = await Promise.all([
        listEntities(),
        listEntityRelationships(),
        listRelationshipSuggestions("pending"),
      ]);
      setEntities(es);
      setRelationships(rs);
      setSuggestions(ss);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    }
  }, []);

  useEffect(() => {
    // Initial data fetch — external-system sync, the intended use of an effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const entityName = useCallback(
    (id: number) =>
      entities.find((e) => e.id === id)?.canonical_name ?? `entity #${id}`,
    [entities]
  );

  const removeRel = useCallback(
    async (id: number) => {
      try {
        await deleteRelationship(id);
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Delete failed");
      }
    },
    [load]
  );

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-semibold text-xl tracking-tight">
            Entity relationships
          </h1>
          <p className="text-ink-muted text-sm">
            Graph that drives catalyst spillover propagation. Deleting an edge
            stops the LLM from considering it on future alerts.
          </p>
        </div>
        <Button onClick={load} size="sm" variant="secondary">
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </Button>
      </div>

      {error && <p className="text-destructive text-sm">{error}</p>}

      <section className="space-y-3">
        <h2 className="font-medium text-base">
          Pending suggestions ({suggestions.length})
        </h2>
        {suggestions.length === 0 && (
          <p className="text-ink-muted text-sm">
            Nothing pending. Suggestions appear when a new company is tracked or
            the backfill script is run.
          </p>
        )}
        {suggestions.map((sug) => (
          <SuggestionCard
            busy={busy === sug.id}
            key={sug.id}
            onDecide={async (decision, keys, autoTrack) => {
              setBusy(sug.id);
              try {
                await decideRelationshipSuggestion(sug.id, {
                  decision,
                  accepted_keys: keys,
                  auto_track: autoTrack,
                });
                await load();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Decision failed");
              } finally {
                setBusy(null);
              }
            }}
            suggestion={sug}
          />
        ))}
      </section>

      <section className="space-y-3">
        <h2 className="font-medium text-base">
          Active relationships ({relationships.length})
        </h2>
        {relationships.length === 0 && (
          <p className="text-ink-muted text-sm">No relationships defined.</p>
        )}
        {relationships.map((rel) => (
          <Card className="flex items-center justify-between p-3" key={rel.id}>
            <div className="flex flex-col gap-1 text-sm">
              <div>
                <span className="font-medium">{entityName(rel.from_entity_id)}</span>{" "}
                <Badge variant="neutral">{KIND_LABEL[rel.kind]}</Badge>{" "}
                <span className="font-medium">{entityName(rel.to_entity_id)}</span>
              </div>
              {rel.notes && (
                <div className="text-ink-muted text-xs">{rel.notes}</div>
              )}
            </div>
            <Button
              onClick={() => removeRel(rel.id)}
              size="sm"
              variant="ghost"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete
            </Button>
          </Card>
        ))}
      </section>
    </div>
  );
}

function SuggestionCard({
  suggestion,
  busy,
  onDecide,
}: {
  suggestion: RelationshipSuggestion;
  busy: boolean;
  onDecide: (
    decision: "accept" | "reject",
    keys: string[],
    autoTrack: boolean
  ) => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [autoTrack, setAutoTrack] = useState(true);
  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };
  // Empty preflight: the LLM ran and found nothing strategic to add
  // (no parent, JV partner, or 13D-class holder). The card has no
  // checkboxes to tick, so "Accept selected" would be a no-op. Render
  // a single Dismiss action instead and label the state honestly.
  const isEmpty =
    (suggestion.payload.parents?.length ?? 0) === 0 &&
    (suggestion.payload.joint_venture_partners?.length ?? 0) === 0 &&
    (suggestion.payload.major_shareholders?.length ?? 0) === 0;
  return (
    <Card className="space-y-3 p-3">
      <div className="flex items-start justify-between">
        <div>
          <div className="font-medium text-sm">
            {suggestion.payload.entity.canonical_name}
          </div>
          {suggestion.payload.entity.summary && (
            <p className="mt-1 text-ink-muted text-xs">
              {suggestion.payload.entity.summary}
            </p>
          )}
        </div>
        <Badge variant="neutral">{suggestion.source}</Badge>
      </div>

      {isEmpty && (
        <p className="text-ink-muted text-xs">
          Preflight found no strategic parent, JV partner, or 13D-class
          shareholder for this company. Nothing to add — Dismiss to clear
          this entry.
        </p>
      )}

      {BUCKET_KINDS.map(({ bucket, label }) => {
        const items = suggestion.payload[bucket];
        if (!items || items.length === 0) return null;
        return (
          <div className="space-y-1" key={bucket}>
            <div className="text-ink-muted text-xs uppercase tracking-wide">
              {label}
            </div>
            {items.map((item: PreflightEntity, idx: number) => {
              const key = `${bucket}:${idx}`;
              return (
                <label
                  className="flex cursor-pointer items-start gap-2 rounded-md p-2 text-sm hover:bg-secondary"
                  key={key}
                >
                  <Checkbox
                    checked={selected.has(key)}
                    onCheckedChange={() => toggle(key)}
                  />
                  <div className="flex-1">
                    <div className="font-medium">
                      {item.canonical_name}
                      {item.ticker && (
                        <span className="ml-2 font-mono text-ink-muted text-xs">
                          {formatTicker(item.ticker)} · {item.exchange}
                        </span>
                      )}
                    </div>
                    {item.summary && (
                      <div className="text-ink-muted text-xs">{item.summary}</div>
                    )}
                  </div>
                </label>
              );
            })}
          </div>
        );
      })}

      {suggestion.payload.sources.length > 0 && (
        <div className="text-ink-muted text-xs">
          Sources:{" "}
          {suggestion.payload.sources.map((s) => (
            <a
              className="mr-2 inline-flex items-center gap-1 underline"
              href={s.url}
              key={s.url}
              rel="noreferrer"
              target="_blank"
            >
              {s.name}
              <ExternalLink className="h-3 w-3" />
            </a>
          ))}
        </div>
      )}

      <div className="flex items-center justify-between border-t pt-2">
        {!isEmpty && (
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <Checkbox
              checked={autoTrack}
              onCheckedChange={(v) => setAutoTrack(!!v)}
            />
            Auto-track accepted entities as parent-only
          </label>
        )}
        <div className="ml-auto flex gap-2">
          <Button
            disabled={busy}
            onClick={() => onDecide("reject", [], false)}
            size="sm"
            variant="ghost"
          >
            <X className="h-3.5 w-3.5" />
            {isEmpty ? "Dismiss" : "Reject"}
          </Button>
          {!isEmpty && (
            <Button
              disabled={busy || selected.size === 0}
              onClick={() =>
                onDecide("accept", Array.from(selected), autoTrack)
              }
              size="sm"
            >
              <Check className="h-3.5 w-3.5" />
              Accept selected
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}

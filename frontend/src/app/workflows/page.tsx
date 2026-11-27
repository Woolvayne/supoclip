"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { AlertCircle, Plus, Trash2 } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  normalizeChannelInput,
  workflowLabel,
  type AutoClipWorkflow,
} from "@/lib/auto-clip";

const PAGE = "mx-auto w-full max-w-3xl px-4 py-8 sm:px-8 md:py-10";
const SECTION_HEADING = "mb-3 text-sm font-medium uppercase tracking-wide text-muted-foreground";

function formatWhen(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "";
}

export default function WorkflowsPage() {
  const t = useTranslations("workflows");
  const [workflows, setWorkflows] = useState<AutoClipWorkflow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [channelUrl, setChannelUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setWorkflows(null);
    setLoadError(null);
    try {
      const response = await fetch("/api/auto-clip", { cache: "no-store" });
      const data = (await response.json()) as {
        workflows?: AutoClipWorkflow[];
        detail?: string;
      };
      if (!response.ok || !Array.isArray(data.workflows)) {
        throw new Error(data.detail || t("loadError"));
      }
      setWorkflows(data.workflows);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : t("loadError"));
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const add = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      const channel = normalizeChannelInput(channelUrl);
      if (!channel || saving) return;
      setSaving(true);
      setAddError(null);
      try {
        const response = await fetch("/api/auto-clip", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel_url: channel }),
        });
        const data = (await response.json()) as {
          workflow?: AutoClipWorkflow;
          detail?: string;
        };
        if (!response.ok || !data.workflow) {
          throw new Error(data.detail || t("addError"));
        }
        setWorkflows((current) => [data.workflow!, ...(current ?? [])]);
        setChannelUrl("");
      } catch (error) {
        setAddError(error instanceof Error ? error.message : t("addError"));
      } finally {
        setSaving(false);
      }
    },
    [channelUrl, saving, t],
  );

  const setEnabled = useCallback(async (workflow: AutoClipWorkflow, enabled: boolean) => {
    setBusyId(workflow.id);
    setLoadError(null);
    try {
      const response = await fetch(`/api/auto-clip/${workflow.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      const data = (await response.json()) as { workflow?: AutoClipWorkflow };
      if (!response.ok || !data.workflow) {
        throw new Error(t("loadError"));
      }
      setWorkflows(
        (current) =>
          current?.map((item) => (item.id === workflow.id ? data.workflow! : item)) ?? null,
      );
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : t("loadError"));
    } finally {
      setBusyId(null);
    }
  }, [t]);

  const remove = useCallback(
    async (workflow: AutoClipWorkflow) => {
      if (!window.confirm(t("deleteConfirm"))) return;
      setBusyId(workflow.id);
      try {
        const response = await fetch(`/api/auto-clip/${workflow.id}`, { method: "DELETE" });
        if (!response.ok) throw new Error(t("loadError"));
        setWorkflows((current) => current?.filter((item) => item.id !== workflow.id) ?? null);
      } catch (error) {
        setLoadError(error instanceof Error ? error.message : t("loadError"));
      } finally {
        setBusyId(null);
      }
    },
    [t],
  );

  return (
    <main className={PAGE}>
      <h1 className="text-2xl font-semibold tracking-tight">{t("title")}</h1>
      <p className="mt-1 text-muted-foreground">{t("subtitle")}</p>

      <form onSubmit={add} className="mt-6 rounded-xl border bg-background p-4">
        <Label htmlFor="auto-clip-channel" className={SECTION_HEADING}>
          {t("channelLabel")}
        </Label>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            id="auto-clip-channel"
            value={channelUrl}
            onChange={(event) => setChannelUrl(event.target.value)}
            placeholder={t("channelPlaceholder")}
            autoComplete="off"
            className="flex-1"
          />
          <Button type="submit" disabled={saving || !channelUrl.trim()}>
            <Plus className="size-4" />
            {saving ? t("adding") : t("add")}
          </Button>
        </div>
        {addError ? (
          <Alert variant="destructive" className="mt-3">
            <AlertCircle className="size-4" />
            <AlertDescription>{addError}</AlertDescription>
          </Alert>
        ) : null}
      </form>

      <h2 className={SECTION_HEADING}>{t("listHeading")}</h2>
      {loadError ? (
        <Alert variant="destructive">
          <AlertCircle className="size-4" />
          <AlertDescription>{loadError}</AlertDescription>
        </Alert>
      ) : null}

      {workflows === null ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : workflows.length === 0 ? (
        <div className="rounded-xl border border-dashed bg-background p-8 text-center text-muted-foreground">
          {t("empty")}
        </div>
      ) : (
        <ul className="space-y-3">
          {workflows.map((workflow) => (
            <li
              key={workflow.id}
              className="flex flex-col gap-3 rounded-xl border bg-background p-4 sm:flex-row sm:items-start sm:justify-between"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate font-medium">{workflowLabel(workflow)}</span>
                  <Badge variant={workflow.enabled ? "secondary" : "outline"}>
                    {workflow.enabled ? t("on") : t("off")}
                  </Badge>
                </div>
                <p className="mt-1 truncate text-xs text-muted-foreground">
                  {workflow.channel_url}
                </p>
                <p className="mt-2 text-xs text-muted-foreground">
                  {t("clipped", { count: workflow.videos_clipped })}
                  {" · "}
                  {workflow.last_checked_at
                    ? t("lastChecked", { when: formatWhen(workflow.last_checked_at) })
                    : t("never")}
                </p>
                {workflow.last_error ? (
                  <p className="mt-2 text-xs text-destructive">
                    {t("errorLabel")}: {workflow.last_error}
                  </p>
                ) : null}
              </div>
              <div className="flex shrink-0 items-center gap-3">
                <Label htmlFor={`auto-clip-${workflow.id}`} className="sr-only">
                  {t("enabledLabel")}
                </Label>
                <Switch
                  id={`auto-clip-${workflow.id}`}
                  checked={workflow.enabled}
                  disabled={busyId === workflow.id}
                  onCheckedChange={(enabled) => void setEnabled(workflow, enabled)}
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={busyId === workflow.id}
                  onClick={() => void remove(workflow)}
                  aria-label={t("delete")}
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}

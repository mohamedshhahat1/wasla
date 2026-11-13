"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { unwrap, type components } from "@wasla/api-client";
import { Button, Checkbox, Dialog } from "@wasla/ui";

import { FormAlert } from "../../auth/form-alert";
import { useSession } from "../../session-provider";
import { useChannelsChanged } from "./queries";

type Capacity = components["schemas"]["ChannelCapacityRead"];
type Connection = components["schemas"]["ChannelConnectionRead"];
type Account = components["schemas"]["WhatsAppAccountResponse"];

/**
 * The owner's choice of which connections stay (CN-07): exactly the new limit, stated; posted with
 * the revision the capacity read named; the list and the gates read again after.
 */
export function SelectionDialog({
  open,
  onOpenChange,
  capacity,
  connections,
  accounts,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  capacity: Capacity;
  connections: Connection[];
  accounts: Map<string, Account>;
}) {
  const t = useTranslations("app");
  const { session } = useSession();
  const changed = useChannelsChanged();
  const keep = capacity.reduction?.target_general_limit ?? capacity.scheduled?.general_limit ?? 0;
  const revision = capacity.reduction?.revision ?? capacity.selection_revision ?? 0;
  const [chosen, setChosen] = useState<string[]>(() => capacity.preselected.slice(0, keep));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const toggle = (id: string, on: boolean) =>
    setChosen((current) => (on ? [...current, id] : current.filter((item) => item !== id)));

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await unwrap(
        session.api.POST("/api/v1/billing/channel-capacity/selection", {
          body: { keep: chosen, expected_revision: revision },
        }),
      );
      await changed();
      onOpenChange(false);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("selection.title")}
      description={t("selection.body", { count: keep })}
      closeLabel={t("close")}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {t("cancel")}
          </Button>
          <Button busy={busy} disabled={chosen.length !== keep} onClick={() => void submit()}>
            {t("selection.submit")}
          </Button>
        </>
      }
    >
      <fieldset className="m-0 flex flex-col gap-2 border-0 p-0" data-testid="selection">
        <legend className="mb-2 text-sm font-medium text-text">{t("selection.keep")}</legend>
        {connections.map((connection) => {
          const account = accounts.get(connection.id);
          const id = `keep-${connection.id}`;
          return (
            <div key={connection.id} className="flex items-center gap-2">
              <Checkbox
                id={id}
                checked={chosen.includes(connection.id)}
                disabled={!chosen.includes(connection.id) && chosen.length >= keep}
                onCheckedChange={(value) => toggle(connection.id, value === true)}
              />
              <label htmlFor={id} className="text-md text-text">
                <bdi dir="ltr">
                  {account?.display_phone_number ?? connection.external_account_id}
                </bdi>
                {account?.display_name ? (
                  <span className="text-text-muted">
                    {" · "}
                    <bdi>{account.display_name}</bdi>
                  </span>
                ) : null}
              </label>
            </div>
          );
        })}
        <p className="m-0 mt-2 text-sm text-text-muted" aria-live="polite">
          {t("selection.chosen", { chosen: chosen.length, count: keep })}
        </p>
      </fieldset>
      <div className="mt-4">
        <FormAlert error={error} />
      </div>
    </Dialog>
  );
}

/**
 * Registry of the notification channel types the Settings page can create.
 *
 * The channel union in `services/notificationsService` is closed, and until
 * now every place that needed to know what a channel *is* re-derived it: the
 * settings page had an `if`-chain mapping the selected type onto request
 * fields, and anything that wanted a label repeated that mapping. Adding a
 * fourth channel meant editing each of those by hand and hoping none of them
 * disagreed about which fields exist.
 *
 * One entry per channel type, keyed by the union itself, so the compiler
 * refuses to build until a new type has a spec.
 *
 * @module services/channelRegistry
 */

import { CreateChannelRequest, NotificationChannelType } from "./notificationsService";

/** How a channel field is rendered by the settings form. */
export type ChannelFieldType = "text" | "secret";

/**
 * One configurable field of a channel type.
 *
 * `name` is the request field it fills, so the form and the request payload
 * cannot drift apart.
 */
export interface ChannelFieldSpec {
  /** Request field this input fills (e.g. `chat_id`). */
  name: keyof Omit<CreateChannelRequest, "channel_type">;
  /** Label shown next to the input. */
  label: string;
  /** Input type: `secret` for values that must not stay readable on screen. */
  type: ChannelFieldType;
  /** Shown when the field is empty. */
  placeholder?: string;
  /** Helper copy under the input. */
  help?: string;
}

/**
 * Form values for the create-channel request.
 *
 * Deliberately loose: the registry decides which values a channel type
 * actually reads, so a new channel never has to widen this type.
 */
export type ChannelFormValues = Record<string, string | undefined>;

/** How one channel type is labelled, configured and sent. */
export interface ChannelSpec {
  /** Human-readable channel name for the picker. */
  label: string;
  /** One-line description shown next to the picker. */
  description: string;
  /** Inputs this channel type needs, in render order. */
  fields: ChannelFieldSpec[];
  /**
   * Build the create request from the supplied form values.
   *
   * Copies `name` and exactly the fields this channel type declares — and only
   * the ones the form actually filled in — so a telegram request never
   * carries a stray `url` field.
   */
  buildRequest(values: ChannelFormValues): CreateChannelRequest;
}

/** Copy every declared field that the form values actually carry. */
function buildRequest(channelType: NotificationChannelType, fields: ChannelFieldSpec[]) {
  return (values: ChannelFormValues): CreateChannelRequest => {
    const patch: Partial<CreateChannelRequest> = {};

    for (const field of fields) {
      const value = values[field.name];
      if (value !== undefined) patch[field.name] = value;
    }
    return { channel_type: channelType, ...patch };
  };
}

/** Assemble one registry entry from its label and field list. */
function spec(
  label: string,
  description: string,
  fields: ChannelFieldSpec[],
  channelType: NotificationChannelType,
): ChannelSpec {
  return { label, description, fields, buildRequest: buildRequest(channelType, fields) };
}

/** Every channel type shares the optional nickname input. */
const NAME_FIELD: ChannelFieldSpec = {
  name: "name",
  label: "Name",
  type: "text",
  placeholder: "Optional nickname",
};

export const CHANNEL_REGISTRY: Record<NotificationChannelType, ChannelSpec> = {
  telegram: spec(
    "Telegram",
    "Bot chat id — the backend posts through your own bot token.",
    [
      NAME_FIELD,
      {
        name: "chat_id",
        label: "Chat ID",
        type: "text",
        placeholder: "123456789",
        help: "Numeric chat id, e.g. from @userinfobot.",
      },
    ],
    "telegram",
  ),
  discord: spec(
    "Discord",
    "Incoming webhook URL for the channel the alerts land in.",
    [
      NAME_FIELD,
      {
        name: "webhook_url",
        label: "Webhook URL",
        type: "text",
        placeholder: "https://discord.com/api/webhooks/…",
      },
    ],
    "discord",
  ),
  webhook: spec(
    "Webhook",
    "Your own endpoint, optionally HMAC-signed with a shared secret.",
    [
      NAME_FIELD,
      {
        name: "url",
        label: "URL",
        type: "text",
        placeholder: "https://example.com/hooks/polymarket",
      },
      {
        name: "secret",
        label: "Signing secret",
        type: "secret",
        placeholder: "Optional — never returned by the API",
      },
    ],
    "webhook",
  ),
};

/** Channel types in registry order, for pickers. */
export const CHANNEL_TYPES = Object.keys(CHANNEL_REGISTRY) as NotificationChannelType[];

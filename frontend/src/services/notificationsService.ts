/**
 * Notification center endpoints under `/api/notifications`.
 *
 * Three groups: channel management (`/api/notifications/channels`) for
 * Telegram/Discord/webhook targets (config is never returned by the API),
 * the in-app alert feed (`/api/notifications`) with unread state and
 * mark-as-read, and per-event-type routing preferences. Also exposes the
 * test-alert trigger used by the Settings page test button.
 *
 * @module services/notificationsService
 */

import { apiClient } from "./apiClient";

export type NotificationChannelType = "telegram" | "discord" | "webhook";

export interface NotificationChannel {
  id: number;
  channel_type: NotificationChannelType;
  name: string;
  is_active: boolean;
  created_at: string | null;
}

export interface CreateChannelRequest {
  channel_type: NotificationChannelType;
  name?: string;
  /** Telegram: bot chat id */
  chat_id?: string;
  /** Discord: webhook URL */
  webhook_url?: string;
  /** Generic webhook: target URL */
  url?: string;
  /** Generic webhook: optional HMAC signing secret */
  secret?: string;
}

export interface NotificationEvent {
  id: number;
  event_type: string;
  trader_wallet: string;
  market_id: string;
  token_id: string;
  side: string;
  size: number;
  price: number;
  read_at: string | null;
  created_at: string;
  unread: boolean;
}

export interface NotificationPreference {
  event_type: string;
  channel_ids: number[];
  enabled: boolean;
  is_default?: boolean;
}

export interface UpdatePreferencesRequest {
  preferences: NotificationPreference[];
}

export interface TestAlertRequest {
  channel_id?: number;
  message?: string;
}

export interface TestAlertResponse {
  status: string;
  reason?: string;
  deliveries?: unknown;
}

export const listNotificationChannels = () =>
  apiClient.get<NotificationChannel[]>("/api/notifications/channels");

export const createNotificationChannel = (request: CreateChannelRequest) =>
  apiClient.post<NotificationChannel>("/api/notifications/channels", request);

export const deleteNotificationChannel = (id: number) =>
  apiClient.delete<void>(`/api/notifications/channels/${id}`);

export const listNotifications = (unreadOnly = false, limit = 50) =>
  apiClient.get<NotificationEvent[]>("/api/notifications", {
    params: { unread_only: unreadOnly, limit },
  });

export const markNotificationRead = (id: number) =>
  apiClient.patch<NotificationEvent>(`/api/notifications/${id}/read`);

export const getNotificationPreferences = () =>
  apiClient.get<NotificationPreference[]>("/api/notifications/preferences");

export const updateNotificationPreferences = (preferences: NotificationPreference[]) =>
  apiClient.put<NotificationPreference[]>("/api/notifications/preferences", { preferences });

export const sendTestAlert = (request: TestAlertRequest) =>
  apiClient.post<TestAlertResponse>("/api/notifications/test", request);

export const notificationsService = {
  listNotificationChannels,
  createNotificationChannel,
  deleteNotificationChannel,
  listNotifications,
  markNotificationRead,
  getNotificationPreferences,
  updateNotificationPreferences,
  sendTestAlert,
};

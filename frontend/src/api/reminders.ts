/**
 * Reminders — `/api/v1/reminders/*` (P7).
 *
 * **Online only.** Reminder generation and delivery are server-only: nothing
 * here enters the P5 outbox, and no reminder op type exists in the offline
 * mutation registry. Offline the screen says so rather than showing a stage or a
 * balance it cannot vouch for.
 *
 * **Nothing here decides anything.** The client cannot create a reminder, choose
 * a stage, name a recipient or set an amount — there is no endpoint that would
 * let it. It reads a work list the server derived and, at most, asks the server
 * to re-attempt a delivery that failed.
 *
 * **No client-side money.** Every figure below is an integer count of minor
 * units the server computed; `formatMoney` renders it and nothing adds it up.
 */

import { request } from "./client";
import type { OperationEnvelope } from "./operation";
import type { OperationResult, ReminderDetail, ReminderOverview } from "./types";

export function getReminderOverview(limit = 200): Promise<ReminderOverview> {
  return request<ReminderOverview>(`/reminders?limit=${limit}`);
}

export function getReminder(reminderId: string): Promise<ReminderDetail> {
  return request<ReminderDetail>(`/reminders/${reminderId}`);
}

/**
 * Re-attempt a delivery that failed.
 *
 * Not "send another reminder": it re-dispatches a stage that already exists, and
 * the server re-reads the authoritative balance first — so a customer who has
 * paid since gets the stage cancelled instead of a message. The `operation_id`
 * is generated once at the click, so a lost response replays rather than sending
 * a second time.
 */
export function sendReminder(
  reminderId: string,
  envelope: OperationEnvelope<Record<string, never>>,
): Promise<OperationResult<ReminderDetail>> {
  return request<OperationResult<ReminderDetail>>(`/reminders/${reminderId}/send`, {
    method: "POST",
    body: { operation_id: envelope.operation_id },
  });
}

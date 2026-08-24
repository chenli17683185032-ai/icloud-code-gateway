export function logEvent(
  event: string,
  fields: Record<string, string | number | boolean | null | undefined> = {},
): void {
  console.info(
    JSON.stringify({
      level: "info",
      event,
      timestamp: new Date().toISOString(),
      ...fields,
    }),
  );
}

export function scannedFileSize(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

export function formatScannedFileSize(value: unknown): string {
  const size = scannedFileSize(value);
  return size === undefined ? "Не зафиксирован" : `${size.toLocaleString("ru-RU")} Б`;
}

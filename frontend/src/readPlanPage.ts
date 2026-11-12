import { ApiError } from "./api";

/** Retry one timed-out plan read without discarding earlier pages or repeating writes. */
export async function readPlanPage<T>(
  read: () => Promise<T>,
  active: () => boolean,
  onRetry: () => void,
): Promise<T> {
  try {
    return await read();
  } catch (error) {
    if (
      !(error instanceof ApiError) ||
      error.status !== 408 ||
      error.code !== "read_timeout" ||
      !active()
    )
      throw error;
    onRetry();
    if (!active()) throw error;
    return read();
  }
}

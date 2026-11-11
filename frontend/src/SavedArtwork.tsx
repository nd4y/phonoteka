import { useState } from "react";

export default function SavedArtwork({
  trackId,
  proposalId,
}: {
  trackId: string;
  proposalId: string;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <figure>
      {failed ? (
        <p role="alert" className="warning-text">
          Не удалось показать сохранённую обложку. Обновите карточку перед записью файла.
        </p>
      ) : (
        <img
          src={`/api/tracks/${encodeURIComponent(trackId)}/proposals/${encodeURIComponent(proposalId)}/art`}
          alt="Обложка из сохранённого предложения"
          onError={() => setFailed(true)}
        />
      )}
      <figcaption>Сохранённое предложение - будет записано в выбранный слот.</figcaption>
    </figure>
  );
}

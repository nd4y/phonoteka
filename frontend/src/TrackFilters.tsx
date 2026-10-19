import { Search, SlidersHorizontal, X } from 'lucide-react';

type TrackFiltersProps = {
  search: string;
  onSearch: (value: string) => void;
  filter: string;
  onFilter: (value: string) => void;
  compact?: boolean;
  searchOnly?: boolean;
  searchLabel?: string;
};

export function TrackFilters({ search, onSearch, filter, onFilter, compact = false, searchOnly = false, searchLabel = 'Поиск по коллекции' }: TrackFiltersProps) {
  return <div className={`filters ${compact ? 'compact-filters' : ''}`}><label className="search-field"><Search size={18} /><input maxLength={200} aria-label={searchLabel} placeholder="Найти трек, исполнителя, альбом" value={search} onChange={event => onSearch(event.target.value)} />{search && <button className="icon-button" onClick={() => onSearch('')} aria-label="Очистить поиск"><X size={15} /></button>}</label>{!searchOnly && <label className="filter-select"><SlidersHorizontal size={16} /><select aria-label="Фильтр треков" value={filter} onChange={event => onFilter(event.target.value)}><option value="all">Все записи</option><option value="cue">Альбомы CUE</option><option value="review">Нужно проверить</option><option value="missing_lyrics">Без текста</option><option value="missing_art">Без обложки</option><option value="published">Опубликованы</option></select></label>}</div>;
}

export function PublishedTrackSearch({ search, onSearch, onPage }: { search: string; onSearch: (value: string) => void; onPage: (value: number) => void }) {
  return <TrackFilters searchOnly searchLabel="Поиск по готовой коллекции" search={search} onSearch={value => { onSearch(value); onPage(1); }} filter="published" onFilter={() => {}} />;
}

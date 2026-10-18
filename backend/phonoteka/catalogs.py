"""Optional release catalogs. Disabled until owner configures their data policy."""
import base64
import re
from urllib.parse import urlsplit
from . import media
from .providers import ProviderError,normalized


async def audiodb(providers,query,meta,authorize):
    if not query.get('artist') or not query.get('album'):return []
    document=await providers.get('theaudiodb','https://www.theaudiodb.com/api/v1/json/123/searchalbum.php',{'s':query['artist'],'a':query['album']})
    results=[]
    for album in (document or {}).get('album') or []:
        identity=str(album.get('idAlbum',''))
        if not re.fullmatch(r'\d{1,12}',identity):continue
        tags={k:str(v)[:1000] for k,v in {'album':album.get('strAlbum'),'albumartist':album.get('strArtist'),'date':album.get('intYearReleased'),'genre':album.get('strGenre')}.items() if v}
        exact=normalized(query['artist'])==normalized(album.get('strArtist')) and normalized(query['album'])==normalized(album.get('strAlbum'))
        url='https://www.theaudiodb.com/album/'+identity
        results.append({'id':'audiodb:'+identity,'provider':'theaudiodb','kind':'release','title':album.get('strAlbum',''),'tags':tags,
                        'identification':'text_search','source_url':url,'attribution':'TheAudioDB',
                        'match':{'state':'needs_review','reasons':['confirm_release'] if exact else ['album_differs','confirm_release']}})
        image=album.get('strAlbumThumb') or ''
        if exact and not meta.get('has_art') and providers.settings.theaudiodb_artwork and urlsplit(image).scheme=='https' and urlsplit(image).hostname in {'www.theaudiodb.com','r2.theaudiodb.com'}:
            authorize();raw=await providers.get('theaudiodb',image,binary=True)
            if raw:
                mime,_,_=media._cover(base64.b64decode(raw))
                results.append({'id':'audiodb-art:'+identity,'provider':'theaudiodb','kind':'cover','cover':'data:'+mime+';base64,'+raw,
                                'source_url':url,'attribution':'TheAudioDB','match':{'state':'needs_review','reasons':['confirm_album_cover','confirm_release']}})
        if len(results)>=8:break
    return results


async def discogs(providers,query,meta,authorize):
    file=providers.settings.discogs_token_file
    if not file or not file.is_file():raise ProviderError('key_required','discogs','Требуется личный токен Discogs')
    if file.stat().st_size>4096:raise ProviderError('invalid_configuration','discogs','Некорректный файл токена')
    token=file.read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,200}',token):raise ProviderError('invalid_configuration','discogs','Некорректный токен')
    if not query.get('artist') or not query.get('album'):return []
    document=await providers.get('discogs','https://api.discogs.com/database/search',{'artist':query['artist'],'release_title':query['album'],'type':'release','per_page':8},headers={'Authorization':'Discogs token='+token})
    results=[]
    for release in (document or {}).get('results',[])[:3]:
        identity=str(release.get('id',''))
        if not re.fullmatch(r'\d{1,12}',identity):continue
        # Search results describe releases. They must never replace a track title.
        authorize();detail=await providers.get('discogs','https://api.discogs.com/releases/'+identity,headers={'Authorization':'Discogs token='+token})
        if not isinstance(detail,dict) or not detail.get('title'):continue
        tags={'album':str(detail['title'])[:1000]}
        if re.fullmatch(r'\d{4}',str(detail.get('year',''))):tags['date']=str(detail['year'])
        results.append({'id':'discogs:'+identity,'provider':'discogs','kind':'release','title':str(release.get('title',''))[:1000],'tags':tags,
                        'release':{key:detail.get(key) for key in ('country','year','formats','labels','tracklist')},'identification':'text_search',
                        'source_url':'https://www.discogs.com/release/'+identity,'attribution':'Discogs',
                        'match':{'state':'needs_review','reasons':['confirm_release']}})
    return results


async def enrich(providers,result,query,meta,authorize):
    for name,enabled,call in [('theaudiodb',providers.settings.theaudiodb_enabled,audiodb),('discogs',providers.settings.discogs_enabled,discogs)]:
        if not enabled:continue
        authorize()
        try:
            candidates=await call(providers,query,meta,authorize)
            result['candidates'].extend(candidates);result['sources'].append({'provider':name,'state':'candidates' if candidates else 'no_match'})
        except (ProviderError,media.MediaError) as exc:
            if exc.code=='provider_unavailable':providers.unavailable(name)
            result['sources'].append({'provider':name,'state':exc.code})

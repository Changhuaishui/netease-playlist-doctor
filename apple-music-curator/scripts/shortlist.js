// Compare public catalog candidates with the Mac library index. No Music events.
ObjC.import("Foundation");

function readTracks(path) {
    const file = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null);
    if (!file || file.isNil()) throw new Error("无法读取 " + path);
    const records = JSON.parse(ObjC.unwrap(file)).tracks;
    if (!Array.isArray(records)) throw new Error(path + " 缺少 tracks 数组");
    return records;
}

const folded = new Map();
function key(text) {
    if (typeof text !== "string") return "";
    if (!folded.has(text)) {
        const simplified = ObjC.unwrap($(text).stringByApplyingTransformReverse("Traditional-Simplified", false));
        const normalized = simplified.normalize("NFKC").toLowerCase().replace(/[\p{Z}\p{P}\p{S}\s]/gu, "");
        folded.set(text, normalized);
    }
    return folded.get(text);
}

function titleBase(title) {
    // A version suffix can change the recording. Surface these matches for review.
    return key(title.normalize("NFKC").replace(/[（(\[【].*$/u, "").replace(/\s[-–—]\s.*$/u, ""));
}

function run(argv) {
    if (argv.length < 2 || argv.length > 3) {
        throw new Error("用法: shortlist.js <library-index.json> <catalog-candidates.json> [reference-playlist.json]");
    }
    const byTitle = new Map();
    const library = readTracks(argv[0]);
    const reference = argv[2] ? readTracks(argv[2]) : [];
    for (const track of library.concat(reference)) {
        if (typeof track.name !== "string" || !track.name.trim()) throw new Error("资料库索引有缺少歌名的记录");
        const base = titleBase(track.name) || key(track.name);
        if (!byTitle.has(base)) byTitle.set(base, []);
        byTitle.get(base).push(track);
    }
    const candidates = Array.from(new Map(readTracks(argv[1]).map(t => [t.catalog_id, t])).values());
    const tracks = candidates.map(candidate => {
        if (!candidate.name || !candidate.artist || !candidate.catalog_id) {
            throw new Error("候选需要 name、artist 和 catalog_id");
        }
        const titles = [candidate.name].concat(candidate.title_aliases || []);
        const artists = [candidate.artist].concat(candidate.artist_aliases || []).map(key);
        const matches = new Map();
        const fullTitles = titles.map(key);
        for (const title of titles) {
            for (const track of byTitle.get(titleBase(title) || key(title)) || []) {
                matches.set(track.persistentID, track);
            }
        }
        const matchingTracks = Array.from(matches.values());
        const present = matchingTracks.some(t => fullTitles.includes(key(t.name)) && artists.includes(key(t.artist)));
        return Object.assign({}, candidate, {
            library_check: present ? "same_title_artist" : matches.size ? "review_version_or_artist" : "no_title_match",
            library_matches: matchingTracks
        });
    });
    return JSON.stringify({library_rows: library.length, reference_rows: reference.length, tracks});
}

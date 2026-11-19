// Run with osascript -l JavaScript. Each bulk property read is one Apple event.
const indexFields = ["name", "artist", "album", "duration"];
const signalFields = [
    "genre", "favorited", "disliked", "rating", "ratingKind", "playedCount",
    "playedDate", "skippedCount", "skippedDate", "dateAdded"
];

function jsonValue(value) {
    if (value == null) return null;
    if (value instanceof Date) {
        return Number.isFinite(value.getTime()) ? value.toISOString() : null;
    }
    if (["string", "boolean", "number"].indexOf(typeof value) < 0) {
        throw new Error("Music returned an unexpected property type");
    }
    return value;
}

function values(collection, field, count) {
    const value = collection[field]();
    if (Array.isArray(value)) return value.map(jsonValue);
    if (value == null && count === 0) return [];
    return [jsonValue(value)];
}

function persistentIDs(collection) {
    const count = collection.length;
    if (count === 0) return [];
    const ids = values(collection, "persistentID", count);
    if (ids.length !== count) throw new Error("Incomplete persistent ID column");
    if (!ids.every(id => typeof id === "string" && /^[0-9a-f]+$/i.test(id))) {
        throw new Error("Music returned an item without a valid persistent ID");
    }
    return ids;
}

function records(collection, fields, optionalFields) {
    const ids = persistentIDs(collection);
    const rows = ids.map(persistentID => ({persistentID}));
    const unavailableFields = [];
    if (ids.length === 0) return {rows, unavailableFields};

    for (const field of fields.concat(optionalFields)) {
        let column;
        try {
            column = values(collection, field, ids.length);
            if (column.length !== ids.length) {
                throw new Error("Incomplete column: " + field);
            }
        } catch (error) {
            if (optionalFields.indexOf(field) < 0) throw error;
            // Some Music versions omit a property for an entire result set.
            // Never attach a partial column to tracks by position.
            unavailableFields.push(field);
            column = Array(ids.length).fill(null);
        }
        rows.forEach((row, index) => { row[field] = column[index]; });
    }
    const finalIDs = persistentIDs(collection);
    if (ids.length !== finalIDs.length || !ids.every((id, i) => id === finalIDs[i])) {
        throw new Error("Music changed the collection during the read. Run the command again.");
    }
    return {rows, unavailableFields};
}

function readPlaylists(music) {
    const result = records(music.playlists, ["name"], ["specialKind"]);
    return {playlists: result.rows, unavailableFields: result.unavailableFields};
}

function readPlaylist(music, id) {
    const candidates = music.playlists.whose({persistentID: id.toUpperCase()})();
    if (candidates.length !== 1) throw new Error("Expected exactly one playlist for ID " + id);
    const selected = candidates[0];
    const playlist = {persistentID: selected.persistentID(), name: selected.name()};
    if (playlist.persistentID.toUpperCase() !== id.toUpperCase()) {
        throw new Error("Music returned an unexpected playlist");
    }
    const result = records(selected.tracks, indexFields, signalFields);
    return {playlist, tracks: result.rows, unavailableFields: result.unavailableFields};
}

function run(argv) {
    const command = argv[0];
    if (argv.length === 1 && command === "playlists") {
        return JSON.stringify(readPlaylists(Application("com.apple.Music")));
    }
    if (argv.length === 2 && command === "playlist" && /^[0-9a-f]{1,16}$/i.test(argv[1])) {
        return JSON.stringify(readPlaylist(Application("com.apple.Music"), argv[1]));
    }
    if (argv.length === 1 && command === "index") {
        const music = Application("com.apple.Music");
        const libraries = music.libraryPlaylists;
        if (libraries.length !== 1) throw new Error("Could not identify a unique Music library");
        const result = records(libraries[0].tracks, indexFields, []);
        return JSON.stringify({tracks: result.rows});
    }
    throw new Error("Usage: osascript -l JavaScript library.js playlists | playlist PERSISTENT_ID | index");
}

-- Request-only objective progress reads on the existing game-thread sampler.
-- Only reflected getters; no input, story mutation, UObject cache or hooks.
local M = {}
local refs = require('object_refs')
local ROOT = 'C:\\CaitGPT\\Telemetry\\'
local MAX_FLAGS, MAX_REQUEST_BYTES = 256, 49152
local handled_id = nil
local function safe(f, default)
    local ok, value = pcall(f)
    if ok and value ~= nil then return value end
    return default
end
local function valid(v) return safe(function() return v ~= nil and v:IsValid() end, false) end
local function quote(s)
    return '"' .. tostring(s):gsub('[%c\\"]', function(c)
        if c == '\\' then return '\\\\' end
        if c == '"' then return '\\"' end
        return string.format('\\u%04x', string.byte(c))
    end) .. '"'
end
local function json(v)
    if v == nil then return 'null' end
    if type(v) == 'string' then return quote(v) end
    if type(v) == 'boolean' then return v and 'true' or 'false' end
    if type(v) == 'number' then
        return v == v and math.abs(v) < 1e12 and tostring(v) or 'null'
    end
    if type(v) ~= 'table' then return 'null' end
    local out, keys = {}, {}
    for k in pairs(v) do keys[#keys+1] = k end
    table.sort(keys)
    for _,k in ipairs(keys) do out[#out+1] = quote(k) .. ':' .. json(v[k]) end
    return '{' .. table.concat(out, ',') .. '}'
end
local function request()
    local f = io.open(ROOT .. 'objective-query.request', 'rb')
    if not f then return nil end
    local line = f:read(MAX_REQUEST_BYTES + 1); f:close()
    if not line or #line > MAX_REQUEST_BYTES then return nil end
    local id, expires, flags = line:match('^(%w+)|(%d+)|([%w_,%-]+)$')
    expires = tonumber(expires)
    if not id or #id > 64 or not expires or expires < os.time() or expires > os.time() + 60 then return nil end
    local ids, seen = {}, {}
    if flags ~= '-' then
        for name in flags:gmatch('[^,]+') do
            if #name > 160 or not name:match('^[%w_%-]+$') or seen[name] then return nil end
            ids[#ids+1] = name; seen[name] = true
            if #ids > MAX_FLAGS then return nil end
        end
        if table.concat(ids, ',') ~= flags then return nil end
    end
    return {id=id, flags=ids}
end
local function getter(name)
    return refs.resolve('/Script/EndDataBase.EndDataBaseDataBaseAPI:' .. name)
end
local function inspect(r, sequence, pawn, map_id, timestamp)
    local state = {schema_version=1, request_id=r.id, source='ue4ss_objective_progress',
        source_sequence=sequence, timestamp_utc=timestamp, map_id=map_id,
        observed=false, flags={}, errors={}, input_sent=false}
    if not valid(pawn) then state.reason='field_not_loaded'; return state end
    state.player_actor_id = safe(function() return pawn:GetFullName() end, '')
    local db = refs.resolve('/Script/EndGame.Default__EndDataBaseAPI')
    if not valid(db) or not valid(getter('IsStoryFlag')) or not valid(getter('GetChapterProgress')) then
        state.reason='objective_getters_unavailable'; return state
    end
    local ok, chapter = pcall(function() return db:GetChapterProgress() end)
    if not ok or type(chapter) ~= 'number' or chapter % 1 ~= 0 then
        state.reason='chapter_progress_unreadable'; return state
    end
    state.chapter_progress = chapter
    for _,id in ipairs(r.flags) do
        local read, value = pcall(function() return db:IsStoryFlag(FName(id)) end)
        if read and type(value) == 'boolean' then state.flags[id] = value
        else state.errors[id] = 'story_flag_unreadable' end
    end
    state.observed = next(state.errors) == nil
    if not state.observed then state.reason='objective_progress_incomplete' end
    return state
end
function M.sample(sequence, pawn, map_id, timestamp)
    local r = request()
    if not r or r.id == handled_id then return end
    local state = inspect(r, sequence, pawn, map_id, timestamp)
    local path = ROOT .. 'objective-query.json'
    local f = io.open(path .. '.tmp', 'wb')
    if not f then return end
    f:write(json(state)); f:close()
    -- Never remove the previous response before the complete replacement exists.
    os.remove(path)
    if os.rename(path .. '.tmp', path) then handled_id = r.id end
end
return M

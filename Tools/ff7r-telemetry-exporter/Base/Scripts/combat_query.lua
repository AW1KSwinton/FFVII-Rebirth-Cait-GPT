-- One request-only, read-only combat snapshot on the existing game thread.
-- Source contracts: EndBattleAPI.h, EndPartyAPI.h, EndMenuBPAPI.h.
-- These candidate getters require controlled live acceptance before promotion.
-- No callbacks, input, setters, object-array scans or retained UObject wrappers.
local M = {}
local refs = require('object_refs')
local ROOT = 'C:\\CaitGPT\\Telemetry\\'
local CONTRACT = 'ff7r-combat-readonly-v1'
local MAX_PARTY, MAX_ENEMIES = 16, 64
local handled_id = nil
local enabled = false
local NULL = {}
local ARRAY = {}
local function array() return setmetatable({}, ARRAY) end
local function safe(f, default)
    local ok, v = pcall(f)
    if ok and v ~= nil then return v end
    return default
end
local function valid(v) return safe(function() return v ~= nil and v:IsValid() end, false) == true end
local function text(v)
    local s = type(v) == 'string' and v or safe(function() return v:ToString() end, nil)
    if type(s) ~= 'string' or s == '' or #s > 1024 or s:find('%c') then return nil end
    return s
end
local function identity(v) return valid(v) and text(safe(function() return v:GetFullName() end, nil)) or nil end
local function integer(v, limit)
    return type(v) == 'number' and v == v and v >= 0 and v <= limit and v % 1 == 0
end
local function quote(s)
    return '"' .. s:gsub('[%c\\"]', function(c)
        if c == '\\' then return '\\\\' end
        if c == '"' then return '\\"' end
        return string.format('\\u%04x', string.byte(c))
    end) .. '"'
end
local function json(v)
    if v == nil or v == NULL then return 'null' end
    if type(v) == 'string' then return quote(v) end
    if type(v) == 'boolean' then return v and 'true' or 'false' end
    if type(v) == 'number' then
        if v ~= v or math.abs(v) > 1e9 then return 'null' end
        return v % 1 == 0 and string.format('%.0f', v) or tostring(v)
    end
    local out = {}
    if getmetatable(v) == ARRAY then
        for _, item in ipairs(v) do out[#out + 1] = json(item) end
        return '[' .. table.concat(out, ',') .. ']'
    end
    local keys = {}
    for k in pairs(v) do keys[#keys + 1] = k end
    table.sort(keys)
    for _, k in ipairs(keys) do out[#out + 1] = quote(k) .. ':' .. json(v[k]) end
    return '{' .. table.concat(out, ',') .. '}'
end
local function request()
    local f = io.open(ROOT .. 'combat-query.request', 'rb')
    if not f then return nil end
    local line = f:read(129); f:close()
    if type(line) ~= 'string' or #line > 128 then return nil end
    local id, expires = line:match('^([0-9a-f]+)|(%d+)$')
    expires = tonumber(expires)
    local now = os.time()
    if not id or #id ~= 32 or not expires or expires < now or expires > now + 15 then return nil end
    return id
end
local function library(name)
    local value = refs.resolve('/Script/EndGame.Default__' .. name)
    return valid(value) and value or nil
end
local function reader(api, owner, name, ...)
    -- Explicit names only; no caller-supplied function names or arguments.
    if not api or not valid(refs.resolve('/Script/EndGame.' .. owner .. ':' .. name)) then return nil end
    local args = {...}
    return safe(function() return api[name](api, table.unpack(args)) end, nil)
end
local function measured(row, key, value, limit)
    if integer(value, limit) then row[key] = value
    else row.errors[#row.errors + 1] = key .. '_unreadable' end
end
local function actor_row(actor, battle, menu, player_type, pawn)
    local id = identity(actor)
    if not id then return nil end
    local row = {actor_id=id, chara_id=NULL, player_type=player_type or NULL,
        hp=NULL, max_hp=NULL, mp=NULL, max_mp=NULL, atb=NULL, distance_m=NULL,
        controller_state=NULL, staggered=NULL, pressured=NULL, status_effects=NULL, errors=array()}
    measured(row, 'hp', reader(battle, 'EndBattleAPI', 'GetCharaHP', actor), 1e9)
    measured(row, 'max_hp', reader(battle, 'EndBattleAPI', 'GetHPMax', actor), 1e9)
    measured(row, 'controller_state', reader(battle, 'EndBattleAPI', 'GetCharacterStateType', actor), 39)
    row.chara_id = text(reader(battle, 'EndBattleAPI', 'GetCharaID', actor)) or NULL
    local distance = safe(function() return pawn:GetDistanceTo(actor) end, nil)
    if type(distance) == 'number' and distance == distance and distance >= 0 and distance <= 1e9 then
        row.distance_m = distance / 100
    else row.errors[#row.errors + 1] = 'distance_unreadable' end
    if player_type ~= nil then
        measured(row, 'mp', reader(menu, 'EndMenuBPAPI', 'BPGetPlayerMP', player_type), 1e9)
        measured(row, 'max_mp', reader(menu, 'EndMenuBPAPI', 'BPGetPlayerMaxMP', player_type), 1e9)
    end
    -- GetSpendATBBarTotal is spent ATB, not a current gauge. GetATB in
    -- EndDataObjectBattleAbilityBlueprint is an ability's cost. Neither is read.
    -- No current-ATB/status/pressure/stagger semantics are guessed from them.
    return row
end
local function party_rows(state, party, battle, menu, pawn)
    local count = reader(party, 'EndPartyAPI', 'GetBattleMemberCount')
    if not integer(count, MAX_PARTY) then state.errors[#state.errors + 1] = 'party_count_unavailable'; return end
    local complete, seen = true, {}
    for i = 0, count - 1 do
        local member = reader(party, 'EndPartyAPI', 'GetBattleMember', i)
        local player_type = reader(party, 'EndPartyAPI', 'GetBattleMemberType', i)
        if not integer(player_type, 255) then player_type = nil end
        local row = actor_row(member, battle, menu, player_type, pawn)
        if not row or seen[row.actor_id] then complete = false
        else state.party[#state.party + 1] = row; seen[row.actor_id] = true end
    end
    if not seen[state.controlled_actor_id] then complete = false end
    state.complete.party = complete
    if not complete then state.errors[#state.errors + 1] = 'party_membership_incomplete' end
end
-- What a value is, for diagnostics: the Lua type plus a UE4SS userdata type
-- (UObject, TArray, RemoteUnrealParam...). Only userdata are asked.
local function kind(v)
    if type(v) ~= 'userdata' then return type(v) end
    local ue = safe(function() return v:type() end, nil)
    return type(ue) == 'string' and ('userdata-' .. ue:gsub('[^%w_]', '')) or 'userdata'
end
-- A UE4SS out-parameter or element wrapper holds its value behind :get().
local function unwrap(v)
    local k = kind(v)
    if k == 'userdata-RemoteUnrealParam' or k == 'userdata-LocalUnrealParam' then
        return safe(function() return v:get() end, nil)
    end
    return v
end
local function enemy_array(value, depth)
    -- Native TArray indexing can abort past pcall. Use only the existing
    -- GetArrayNum/ForEach/element:get pattern, or a dense returned Lua table.
    local count = safe(function() return value:GetArrayNum() end, nil)
    local items = {}
    if count ~= nil then
        if not integer(count, MAX_ENEMIES) then return nil, 'tarray:count-' .. tostring(count) end
        local visited = 0
        local ok = pcall(function()
            value:ForEach(function(_, element)
                visited = visited + 1
                if visited <= MAX_ENEMIES then
                    local actor = safe(function() return element:get() end, element)
                    if valid(actor) then items[#items + 1] = actor end
                end
            end)
        end)
        if ok and visited == count and #items == count then return items end
        return nil, string.format('tarray:count-%s:visited-%d:valid-%d:walk-%s', tostring(count), visited, #items, tostring(ok))
    end
    if type(value) ~= 'table' or not integer(#value, MAX_ENEMIES) then
        return nil, 'value:' .. kind(value)
    end
    -- Live 2026-10-20 (Mt. Nibel): the getter returns nothing and fills the
    -- out table with two entries. The table may hold the array itself.
    if #value == 1 and (depth or 0) == 0 then
        -- Asked only of a TArray: a failed member lookup on a UObject
        -- leaks native stack (2026-10-05).
        local inner = unwrap(value[1])
        if kind(inner) == 'userdata-TArray' then return enemy_array(inner, 1) end
    end
    for k in pairs(value) do
        if not integer(k, MAX_ENEMIES) or k < 1 or k > #value then
            return nil, 'table:n-' .. #value .. ':key-' .. type(k)
        end
    end
    for i, entry in ipairs(value) do
        local actor = unwrap(entry)
        if not valid(actor) then
            return nil, string.format('table:n-%d:entry-%d-%s-%s', #value, i, kind(entry), kind(actor))
        end
        items[#items + 1] = actor
    end
    return items
end
local function enemy_rows(state, battle, menu, pawn)
    if not battle or not valid(refs.resolve('/Script/EndGame.EndBattleAPI:GetEnemyMembersRef')) then
        state.errors[#state.errors + 1] = 'enemy_getter_unavailable'; return
    end
    local output = {}
    local ok, returned = pcall(function() return battle:GetEnemyMembersRef(output) end)
    if not ok then state.errors[#state.errors + 1] = 'enemy_array_unreadable'; return end
    -- A void getter leaving its out table empty is not proof of zero enemies:
    -- the binding may not have populated it. A typed/returned array is evidence.
    local value = returned ~= nil and returned or output
    if returned == nil and #output == 0 then
        state.errors[#state.errors + 1] = 'enemy_out_array_unverified'; return
    end
    local items, shape = enemy_array(value)
    if not items then
        state.errors[#state.errors + 1] = 'enemy_array_incomplete'
        -- What the getter handed back, for controlled live acceptance.
        if shape then state.errors[#state.errors + 1] = 'enemy_shape:' .. shape:gsub('[^%w_.:%-]', '') end
        state.errors[#state.errors + 1] = 'enemy_returned:' .. type(returned) .. ':out-' .. tostring(#output)
        return
    end
    local seen = {}
    for _, row in ipairs(state.party) do seen[row.actor_id] = true end
    for _, actor in ipairs(items) do
        local row = actor_row(actor, battle, menu, nil, pawn)
        if not row or seen[row.actor_id] then
            state.enemies = array(); state.errors[#state.errors + 1] = 'enemy_identity_conflict'; return
        end
        state.enemies[#state.enemies + 1] = row; seen[row.actor_id] = true
    end
    state.complete.enemies = true
end
local function inspect(id, sequence, pawn, map_id, pawn_source)
    local actor_id = identity(pawn)
    local world_id = safe(function() return identity(pawn:GetWorld()) end, nil)
    local state = {schema_version=1, producer_contract=CONTRACT, request_id=id, source='ue4ss_combat_query',
        source_sequence=sequence, map_id=map_id, world_id=world_id or NULL, controlled_actor_id=actor_id or NULL,
        controlled_actor_source=type(pawn_source) == 'string' and pawn_source or 'none',
        observed=false, in_battle=NULL, phase='unknown', paused=NULL, party=array(), enemies=array(),
        complete={party=false, enemies=false}, target={observed=false, actor_id=NULL}, errors=array(),
        timestamp_precision='seconds', input_sent=false, input_authorized=false}
    if not actor_id or not world_id or type(map_id) ~= 'string' or map_id == '' then
        state.errors[#state.errors + 1] = 'combat_world_unavailable'; return state
    end
    local battle = library('EndBattleAPI')
    local active = reader(battle, 'EndBattleAPI', 'IsInBattle')
    if type(active) ~= 'boolean' then
        state.errors[#state.errors + 1] = 'battle_state_unavailable'; return state
    end
    local gameplay = refs.resolve('/Script/Engine.Default__GameplayStatics')
    if valid(gameplay) then
        local paused = safe(function() return gameplay:IsGamePaused(pawn) end, nil)
        if type(paused) == 'boolean' then state.paused = paused end
    end
    if active then
        local menu = library('EndMenuBPAPI')
        party_rows(state, library('EndPartyAPI'), battle, menu, pawn)
        enemy_rows(state, battle, menu, pawn)
        -- Call the typed target getter only when the controlled pawn was
        -- independently identified in the typed battle-party membership.
        for _, row in ipairs(state.party) do
            if row.actor_id == actor_id and valid(refs.resolve('/Script/EndGame.EndBattleAPI:GetTargetCharacter')) then
                local ok, target = pcall(function() return battle:GetTargetCharacter(pawn) end)
                if ok then
                    local target_id = identity(target)
                    -- Null means no target; an invalid wrapper is not null evidence.
                    if target == nil or target_id then
                        state.target = {observed=true, actor_id=target_id or NULL}
                    end
                end
            end
        end
    end
    local after = reader(battle, 'EndBattleAPI', 'IsInBattle')
    if after ~= active or identity(pawn) ~= actor_id
            or safe(function() return identity(pawn:GetWorld()) end, nil) ~= world_id then
        state.party, state.enemies = array(), array()
        state.complete = {party=false, enemies=false}
        state.target = {observed=false, actor_id=NULL}
        state.errors[#state.errors + 1] = 'combat_changed_during_sample'
        return state
    end
    state.observed, state.in_battle, state.phase = true, active, active and 'active' or 'inactive'
    return state
end
function M.set_active(active)
    -- Driven by the existing base feed's observed battle-state transitions.
    -- Disabling needs no file checks, getters or retained battle data.
    enabled = active == true
end
function M.sample(sequence, pawn, map_id, pawn_source)
    if not enabled then return end
    local id = request()
    if not id or id == handled_id then return end
    local state = inspect(id, sequence, pawn, map_id, pawn_source)
    state.timestamp_utc = os.date('!%Y-%m-%dT%H:%M:%SZ') -- after the combat getters, never file mtime
    local path = ROOT .. 'combat-query.' .. id .. '.json'
    local f = io.open(path .. '.tmp', 'wb')
    if not f then return end
    local ok = pcall(function() assert(f:write(json(state))); assert(f:flush()); assert(f:close()) end)
    if not ok then pcall(function() f:close() end); return end
    -- The destination is a unique nonce: rename never needs to delete a peer
    -- or an earlier response, and readers never observe partially written JSON.
    if os.rename(path .. '.tmp', path) then handled_id = id end
end
return M

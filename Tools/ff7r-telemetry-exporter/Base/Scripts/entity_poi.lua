-- Optional, request-scoped POI/actor observation on the existing sampler thread.
-- No input, actor changes, native pointer offsets, or asynchronous callbacks.
local M = {}
local refs = require("object_refs")
local ROOT = "C:\\CaitGPT\\Telemetry\\"
local request, cached_actors, ticks = nil, {}, 0
local character_snapshot_request_id, character_snapshot_written = nil, false
local scan_info = {}
local last_prompt_target = nil
local CHARACTER_INVENTORY_POI_ID = "character_inventory"
local CHARACTER_CANDIDATES_POI_ID = "character_candidates"
local CHARACTER_TARGET_POI_ID = "character_target"
local ACTOR_INVENTORY_POI_ID = "actor_inventory"
local TRAVERSAL_GIMMICKS_POI_ID = "traversal_gimmicks"
-- Ladders, climbing walls/pipes, cracks to squeeze through and crawl spaces
-- join navmesh islands the capture leaves apart. None derives from the
-- field-action bases scanned above.
local TRAVERSAL_GIMMICK_CLASSES = {'EndFieldActionActorLadder', 'EndFieldActionClimbingActor',
    'EndFieldActionActorCrack', 'EndFieldActionActorCrouched'}
local TRAVERSAL_GIMMICK_KINDS = {EndFieldActionActorLadder='ladder', EndFieldActionClimbingActor='climbing',
    EndFieldActionActorCrack='crack', EndFieldActionActorCrouched='crouch'}
local MAX_TRAVERSAL_GIMMICKS = 256
local MAX_CLIMBING_POINTS = 64
local MAX_SPLINE_POINTS = 32
local SPLINE_WORLD_SPACE = 1
local CHARACTER_TARGET_RADIUS_M = 10
local MAX_CHARACTER_SCAN = 4096
local MAX_CHARACTER_OUTPUT = 1024
local MAX_ACTOR_OUTPUT = 2048
local function safe(f, fallback)
    local ok, value = pcall(f)
    if ok and value ~= nil then return value end
    return fallback
end
local function valid(v) return safe(function() return v ~= nil and v:IsValid() end, false) end
local function text(v)
    -- Keep reflected conversion attempts inside the protected call. Some
    -- UE4SS userdata tostring metamethods can recurse and overflow the C stack.
    return safe(function()
        if v == nil then return "" end
        local converted_ok, converted = pcall(function() return v:ToString() end)
        if converted_ok and converted ~= nil then
            local string_ok, stringified = pcall(function() return tostring(converted) end)
            if string_ok then return stringified end
        end
        return tostring(v)
    end, "")
end
local function api(n) return safe(function() return StaticFindObject("/Script/EndGame.Default__" .. n) end) end
local function number(v) return type(v) == "number" and v == v and math.abs(v) < 1e9 end
local function position(v)
    if v and number(v.X) and number(v.Y) and number(v.Z) then return {x=v.X,y=v.Y,z=v.Z} end
end
local function actor_rotation(actor)
    return safe(function()
        local v=actor:K2_GetActorRotation()
        if v and number(v.Pitch) and number(v.Yaw) and number(v.Roll) then
            return {pitch=v.Pitch,yaw=v.Yaw,roll=v.Roll}
        end
    end)
end
local function distance(a,b)
    return math.sqrt((a.x-b.x)^2+(a.y-b.y)^2+(a.z-b.z)^2)/100
end
local function quoted(s)
    return '"' .. tostring(s):gsub('[%z\1-\31\\"]', function(c)
        if c == '\\' then return '\\\\' end
        if c == '"' then return '\\"' end
        return string.format('\\u%04x',string.byte(c))
    end) .. '"'
end
local function json(v)
    if v == nil then return 'null' end
    if type(v) == 'string' then return quoted(v) end
    if type(v) == 'boolean' then return v and 'true' or 'false' end
    if type(v) == 'number' then return number(v) and (v==math.floor(v) and string.format('%.0f',v) or string.format('%.6f',v)) or 'null' end
    if type(v) == 'table' then
        local values={}
        local array_value=true
        local highest=0
        local count=0
        for k,_ in pairs(v) do
            if type(k)~='number' or k<1 or k%1~=0 then
                array_value=false
                break
            end
            if k>highest then highest=k end
            count=count+1
        end
        if array_value and highest~=count then array_value=false end
        if array_value then
            for i=1,highest do
                if v[i]==nil then array_value=false;break end
            end
        end
        if array_value then
            for i=1,highest do values[#values+1]=json(v[i]) end
            return '['..table.concat(values,',')..']'
        end
    end
    local values={}
    for k,x in pairs(v) do values[#values+1]=quoted(k)..':'..json(x) end
    return '{'..table.concat(values,',')..'}'
end
local function array(v)
    local values={};for _,x in ipairs(v) do values[#values+1]=json(x) end
    return '['..table.concat(values,',')..']'
end
local function load_request()
    local f=io.open(ROOT..'entity-poi.request','rb'); if not f then return nil end
    local s=f:read(2049);f:close()
    if not s or #s>2048 then return nil end
    local fields={};for x in s:gmatch('([^|]+)') do fields[#fields+1]=x end
    if #fields~=5 then return nil end
    local expires=tonumber(fields[2]);local now=os.time()
    if not expires or expires<now or expires>now+1800 then return nil end
    for _,i in ipairs({1,3,4,5}) do
        if i==4 and fields[5]==CHARACTER_TARGET_POI_ID then
            if #fields[i] < 1 or #fields[i] > 512 or fields[i]:find('[%c|]') then return nil end
        elseif not (i==3 and fields[i]=='*') and not fields[i]:match('^[%w_%-]+$') then
            return nil
        end
    end
    return {id=fields[1],expires=expires,map_id=fields[3],object_id=fields[4],poi_id=fields[5]}
end
local function collect()
    local out,seen={},{}
    scan_info = {}
    for _,class in ipairs({'EndCharacterBase','EndFieldActionActorBase','End2FieldActionActorBase'}) do
        local values=safe(function() return FindAllOf(class) end,{})
        scan_info[class] = type(values)=='table' and #values or -1
        if type(values)~='table' or #values>4096 then return {},false end
        for _,actor in ipairs(values) do
            if valid(actor) then
                local id=safe(function() return actor:GetFullName() end,'')
                if id~='' and id:find('PersistentLevel',1,true) and not id:find('Default__',1,true) and not seen[id] then
                    seen[id]=true;out[#out+1]={actor=actor,id=id,kind=class=='EndCharacterBase' and 'character' or 'field_action'}
                end
            end
            if #out>512 then return {},false end
        end
    end
    return out,true
end
local function collect_character_inventory(battle, battle_class, player_pos, include_detail)
    local values=safe(function() return FindAllOf('EndCharacterBase') end,{})
    if type(values)~='table' or #values>MAX_CHARACTER_SCAN then
        return {complete=false,reason='character_scan_bounded',scan_count=type(values)=='table' and #values or -1,characters={}}
    end
    local rows,seen={},{}
    for _,actor in ipairs(values) do
        if valid(actor) then
            local id=safe(function() return actor:GetFullName() end,'')
            if id~='' and id:find('PersistentLevel',1,true) and not id:find('Default__',1,true) and not seen[id] then
                seen[id]=true
                local is_character=valid(battle_class) and safe(function() return actor:IsA(battle_class) end,false)
                local p=position(safe(function() return actor:K2_GetActorLocation() end))
                local row={
                    id=id,
                    class=text(safe(function() return actor:GetClass():GetFullName() end,'')),
                    actor_name=is_character and text(safe(function() return battle:GetActorName(actor) end,'')) or '',
                    is_player=safe(function() return actor:IsPlayer() end,nil),
                    position=p,rotation=actor_rotation(actor),
                    distance=p and distance(p,player_pos) or nil
                }
                if include_detail then
                    row.chara_id=is_character and text(safe(function() return battle:GetCharaID(actor) end,'')) or ''
                    row.chara_spec=is_character and text(safe(function() return battle:GetCharaSpecName(actor) end,'')) or ''
                end
                rows[#rows+1]=row
                if #rows>=MAX_CHARACTER_OUTPUT then
                    return {complete=false,reason='character_output_bounded',scan_count=#values,characters=rows}
                end
            end
        end
    end
    return {complete=true,reason='',scan_count=#values,characters=rows}
end
local function collect_actor_inventory(battle,battle_class,player_pos)
    local rows,seen={},{}
    local counts={}
    for _,class in ipairs({'EndCharacterBase','EndFieldActionActorBase','End2FieldActionActorBase'}) do
        local values=safe(function() return FindAllOf(class) end,{})
        counts[class]=type(values)=='table' and #values or -1
        if type(values)~='table' or #values>MAX_CHARACTER_SCAN then
            return {complete=false,reason='actor_scan_bounded',scan_count=#rows,scan_counts=counts,actors=rows}
        end
        for _,actor in ipairs(values) do
            if valid(actor) then
                local id=safe(function() return actor:GetFullName() end,'')
                if id~='' and id:find('PersistentLevel',1,true) and not id:find('Default__',1,true) and not seen[id] then
                    seen[id]=true
                    -- The scan class already establishes character-ness; avoid IsA on field-action actors.
                    -- That reflected cross-class check can overflow the native C stack in UE4SS.
                    local is_character=class=='EndCharacterBase'
                    local p=position(safe(function() return actor:K2_GetActorLocation() end))
                    local row={
                        id=id,
                        kind=class=='EndCharacterBase' and 'character' or 'field_action',
                        class=text(safe(function() return actor:GetClass():GetFullName() end,'')),
                        actor_name=is_character and text(safe(function() return battle:GetActorName(actor) end,'')) or '',
                        -- IsPlayer is a character method: the failed lookup on each
                        -- field-action actor leaks C stack until the scan overflows.
                        is_player=is_character and safe(function() return actor:IsPlayer() end,nil) or nil,
                        position=p,rotation=actor_rotation(actor),
                        distance=p and distance(p,player_pos) or nil
                    }
                    if is_character then
                        row.chara_id=text(safe(function() return battle:GetCharaID(actor) end,''))
                        row.chara_spec=text(safe(function() return battle:GetCharaSpecName(actor) end,''))
                    end
                    rows[#rows+1]=row
                    if #rows>=MAX_ACTOR_OUTPUT then
                        return {complete=false,reason='actor_output_bounded',scan_count=#rows,scan_counts=counts,actors=rows}
                    end
                end
            end
        end
    end
    return {complete=true,reason='',scan_count=#rows,scan_counts=counts,actors=rows}
end
local function component_position(component)
    if not valid(component) then return nil end
    return position(safe(function() return component:K2_GetComponentLocation() end))
end
local function short_class(object)
    return text(safe(function() return object:GetClass():GetFName():ToString() end,''))
end
-- Only properties each scanned class declares are read (reflected header
-- dump): a lookup that fails leaks C stack in this UE4SS build.
local function climbing_points(actor)
    local points,count={},0
    local array_value=safe(function() return actor.ClimbingPointArray end)
    if array_value==nil then return points,false end
    local complete=true
    safe(function()
        array_value:ForEach(function(_,element)
            count=count+1
            if count>MAX_CLIMBING_POINTS then complete=false;return true end
            local point=safe(function() return element:get() end)
            if valid(point) then
                points[#points+1]={class=short_class(point),position=component_position(point)}
            end
        end)
        return true
    end)
    return points,complete
end
-- A crack's or crawl space's spline is the path the action moves the
-- character along; its trigger boxes sit only at the start of each side.
local function spline_points(spline)
    if not valid(spline) then return nil end
    local count=safe(function() return spline:GetNumberOfSplinePoints() end,nil)
    if not number(count) then return nil end
    local points={}
    for index=0,math.min(count,MAX_SPLINE_POINTS)-1 do
        points[#points+1]=position(safe(function() return spline:GetLocationAtSplinePoint(index,SPLINE_WORLD_SPACE) end))
    end
    return points
end
local function box(component)
    if not valid(component) then return nil end
    local extent=safe(function() return component:GetScaledBoxExtent() end)
    return {position=component_position(component),extent=position(extent)}
end
local function collect_traversal_gimmicks(player_pos)
    local rows,seen,counts={},{},{}
    for _,class in ipairs(TRAVERSAL_GIMMICK_CLASSES) do
        local values=safe(function() return FindAllOf(class) end,{})
        counts[class]=type(values)=='table' and #values or 0
        if type(values)=='table' then
            for _,actor in ipairs(values) do
                if valid(actor) then
                    local id=safe(function() return actor:GetFullName() end,'')
                    if id~='' and id:find('PersistentLevel',1,true) and not id:find('Default__',1,true) and not seen[id] then
                        seen[id]=true
                        local p=position(safe(function() return actor:K2_GetActorLocation() end))
                        local row={id=id,kind=TRAVERSAL_GIMMICK_KINDS[class],
                            class=short_class(actor),position=p,rotation=actor_rotation(actor),
                            distance=p and distance(p,player_pos) or nil}
                        if row.kind=='ladder' then
                            row.upper=component_position(safe(function() return actor.UpperVolume end))
                            row.lower=component_position(safe(function() return actor.LowerVolume end))
                            row.rung_count=safe(function() return actor.RungCount end,nil)
                        elseif row.kind=='climbing' then
                            row.upper=component_position(safe(function() return actor.UpperVolume end))
                            row.points,row.points_complete=climbing_points(actor)
                        else
                            -- The trigger boxes at either end of the passage.
                            row.enter=component_position(safe(function() return actor.EnterVolume end))
                            row.exit=component_position(safe(function() return actor.ExitVolume end))
                            local spline
                            if row.kind=='crack' then spline=safe(function() return actor.CrackComponent end)
                            else spline=safe(function() return actor.CrouchedComponent end) end
                            row.spline=spline_points(spline)
                            row.impassable_enter=box(safe(function() return actor.DividedImpassableEnterVolume end))
                            row.impassable_exit=box(safe(function() return actor.DividedImpassableExitVolume end))
                        end
                        rows[#rows+1]=row
                        if #rows>=MAX_TRAVERSAL_GIMMICKS then
                            return {complete=false,reason='traversal_gimmick_output_bounded',scan_counts=counts,gimmicks=rows}
                        end
                    end
                end
            end
        end
    end
    return {complete=true,reason='',scan_counts=counts,gimmicks=rows}
end
local function character_row(actor, id, battle, battle_class, player_pos, include_detail)
    local is_character=valid(battle_class) and safe(function() return actor:IsA(battle_class) end,false)
    local actor_name=include_detail and is_character and text(safe(function() return battle:GetActorName(actor) end,'')) or ''
    local p=position(safe(function() return actor:K2_GetActorLocation() end))
    local row={
        id=id,
        class=text(safe(function() return actor:GetClass():GetFullName() end,'')),
        actor_name=actor_name,
        is_player=safe(function() return actor:IsPlayer() end,nil),
        position=p,rotation=actor_rotation(actor),
        distance=p and distance(p,player_pos) or nil
    }
    if include_detail then
        row.chara_id=is_character and text(safe(function() return battle:GetCharaID(actor) end,'')) or ''
        row.chara_spec=is_character and text(safe(function() return battle:GetCharaSpecName(actor) end,'')) or ''
        row.battle_chara_spec_datatable_id=is_character and text(safe(function() return battle:GetBattleCharaSpec_DataTableID(actor) end,'')) or ''
        row.character_name_symbol_index=is_character and safe(function() return battle:GetCharacterNameSymbolIndex(actor) end,nil) or nil
        row.character_state_type=is_character and text(safe(function() return battle:GetCharacterStateType(actor) end,'')) or ''
    end
    return row
end
local function lookup_actor_by_name(battle, actor_name, allow_find_chara_pop_manager)
    local found=nil
    local ok,err=pcall(function()
        found=battle:GetCharaFromActorName(FName(actor_name),allow_find_chara_pop_manager)
    end)
    if not ok then
        return {status='call_error',valid=false,id='',error=safe(function() return tostring(err) end,'')}
    end
    if found==nil then return {status='null_default',valid=false,id=''} end
    if not valid(found) then
        return {status='non_live_reference',valid=false,id=safe(function() return found:GetFullName() end,'')}
    end
    return {status='live_uobject',valid=true,id=safe(function() return found:GetFullName() end,'')}
end
local function lookup_characters_by_id(battle, upper, lower, input_list)
    local output={}
    local result={upper=upper,lower=lower,status='not_called',returned_count=0,valid_count=0,actor_ids={}}
    local returned=nil
    local ok,err=pcall(function()
        returned=battle:GetCharactersFromID(FName(upper),FName(lower),output,input_list)
    end)
    if not ok then
        result.status='call_error'
        result.error=safe(function() return tostring(err) end,'')
        return result
    end
    -- UE4SS may expose an out TArray by mutation or as a returned Lua table.
    -- Accept only a bounded table of live UObjects; never treat an opaque/default
    -- return value as an identity hit.
    if type(output)~='table' or #output==0 then
        if type(returned)=='table' then output=returned end
    end
    if type(output)~='table' then
        result.status=returned==nil and 'null_default' or 'opaque_result'
        return result
    end
    result.returned_count=#output
    for i=1,math.min(#output,16) do
        local actor=output[i]
        if valid(actor) then
            local id=safe(function() return actor:GetFullName() end,'')
            if id~='' and id:find('PersistentLevel',1,true) and not id:find('Default__',1,true) then
                result.actor_ids[#result.actor_ids+1]=id
            end
        end
    end
    result.valid_count=#result.actor_ids
    result.status=result.valid_count>0 and 'live_uobjects' or (#output>0 and 'invalid_references' or 'no_result')
    return result
end
local function collect_character_target(selector, battle, battle_class, player_pos)
    local values=safe(function() return FindAllOf('EndCharacterBase') end,{})
    if type(values)~='table' or #values>MAX_CHARACTER_SCAN then
        return {complete=false,reason='character_scan_bounded',scan_count=type(values)=='table' and #values or -1,target=nil,nearby={}}
    end
    local target_actor_name,lookup_mode,api_chara_id
    target_actor_name=selector:match('^actor_name_global:(.+)$')
    if target_actor_name then lookup_mode='global_only' end
    if not target_actor_name then
        target_actor_name=selector:match('^actor_name_manager:(.+)$')
        if target_actor_name then lookup_mode='allow_find_chara_pop_manager' end
    end
    if not target_actor_name then target_actor_name=selector:match('^actor_name:(.+)$') end
    api_chara_id=selector:match('^chara_id_api:(.+)$')
    local target,nearby,seen=nil,{},{}
    for _,actor in ipairs(values) do
        if valid(actor) then
            local id=safe(function() return actor:GetFullName() end,'')
            if id~='' and id:find('PersistentLevel',1,true) and not id:find('Default__',1,true) and not seen[id] then
                seen[id]=true
                local p=position(safe(function() return actor:K2_GetActorLocation() end))
                local d=p and distance(p,player_pos) or nil
                local is_target=id==selector
                if target_actor_name then
                    local is_character=valid(battle_class) and safe(function() return actor:IsA(battle_class) end,false)
                    local actor_name=is_character and text(safe(function() return battle:GetActorName(actor) end,'')) or ''
                    is_target=actor_name==target_actor_name
                end
                if is_target then
                    target=character_row(actor,id,battle,battle_class,player_pos,true)
                elseif d and d<=CHARACTER_TARGET_RADIUS_M then
                    nearby[#nearby+1]=character_row(actor,id,battle,battle_class,player_pos,false)
                end
            end
        end
    end
    local lookup={}
    if target_actor_name and lookup_mode then
        lookup[lookup_mode]=lookup_actor_by_name(battle,target_actor_name,lookup_mode=='allow_find_chara_pop_manager')
    end
    if api_chara_id then
        local upper,lower=api_chara_id:match('^([^_]+)_(.+)$')
        if upper and lower then
            lookup.by_id=lookup_characters_by_id(battle,upper,lower,values)
        else
            lookup.by_id={status='invalid_chara_id',value=api_chara_id,actor_ids={}}
        end
    end
    if next(lookup)==nil then lookup=nil end
    return {
        complete=target~=nil,
        reason=target and '' or 'character_target_not_found',
        scan_count=#values,
        target=target,
        nearby=nearby,
        lookup=lookup
    }
end
local function write_json(path, payload)
    local f=io.open(path,'wb')
    if not f then return false end
    local ok=pcall(function() f:write(payload);f:flush();f:close() end)
    if not ok then pcall(function() f:close() end) end
    return ok
end
-- Local visibility plus parent visibility/opacity. Unknowns do not authorize.
local function shown(widget)
    local seen={}
    for _=1,24 do
        if not valid(widget) then return nil end
        local id=safe(function() return widget:GetFullName() end,'')
        if id=='' or seen[id] then return nil end
        seen[id]=true
        local visible=safe(function() return widget:IsVisible() end)
        local opacity=safe(function() return widget:GetRenderOpacity() end)
        if type(visible)~='boolean' or not number(opacity) then return nil end
        if not visible or opacity<=0.05 then return false end
        local parent=safe(function() return widget:GetParent() end)
        if not valid(parent) then return true end
        widget=parent
    end
    return nil
end
function M.sample(sequence, player, map_id, prompt_reader, source_timestamp)
    ticks=ticks+1
    if ticks%10==1 then
        local next_request=load_request()
        if not next_request or not request or next_request.id~=request.id then
            cached_actors={};last_prompt_target=nil
            character_snapshot_request_id=nil
            character_snapshot_written=false
        end
        request=next_request
    end
    if not request or request.expires<os.time() or
       (request.map_id~='*' and map_id~=request.map_id) or not valid(player) then return end
    local player_id=player:GetFullName()
    local player_pos=position(player:K2_GetActorLocation());if not player_pos then return end
    local field,battle=api('EndFieldAPI'),api('EndBattleAPI')
    local battle_class=safe(function() return StaticFindObject('/Script/EndGame.EndCharacter') end)
    if request.poi_id==CHARACTER_INVENTORY_POI_ID or request.poi_id==CHARACTER_CANDIDATES_POI_ID then
        if character_snapshot_request_id==request.id and character_snapshot_written then return end
        local detailed=request.poi_id==CHARACTER_INVENTORY_POI_ID
        local inventory=collect_character_inventory(battle,battle_class,player_pos,detailed)
        local state={
            source=detailed and 'ue4ss_character_inventory' or 'ue4ss_character_candidates',
            schema_version=1,
            mode=detailed and 'inventory_snapshot' or 'candidate_snapshot',
            request_id=request.id,
            source_sequence=sequence,
            map_id=map_id,
            player_actor_id=player_id,
            player_position=player_pos,
            timestamp_utc=source_timestamp or os.date('!%Y-%m-%dT%H:%M:%SZ'),
            complete=inventory.complete,
            reason=inventory.reason,
            scan_count=inventory.scan_count,
            characters=inventory.characters
        }
        local path=ROOT..(detailed and 'character-inventory.' or 'character-candidates.')..tostring(sequence%2)..'.json'
        character_snapshot_request_id=request.id
        character_snapshot_written=write_json(path,json(state))
        return character_snapshot_written and state.timestamp_utc or nil
    end
    if request.poi_id==ACTOR_INVENTORY_POI_ID then
        if character_snapshot_request_id==request.id and character_snapshot_written then return end
        local inventory=collect_actor_inventory(battle,battle_class,player_pos)
        local state={
            source='ue4ss_actor_inventory',
            schema_version=1,
            mode='actor_inventory_snapshot',
            request_id=request.id,
            source_sequence=sequence,
            map_id=map_id,
            player_actor_id=player_id,
            player_position=player_pos,
            timestamp_utc=source_timestamp or os.date('!%Y-%m-%dT%H:%M:%SZ'),
            complete=inventory.complete,
            reason=inventory.reason,
            scan_count=inventory.scan_count,
            scan_counts=inventory.scan_counts,
            actors=inventory.actors
        }
        local path=ROOT..'actor-inventory.'..tostring(sequence%2)..'.json'
        character_snapshot_request_id=request.id
        character_snapshot_written=write_json(path,json(state))
        return character_snapshot_written and state.timestamp_utc or nil
    end
    if request.poi_id==TRAVERSAL_GIMMICKS_POI_ID then
        if character_snapshot_request_id==request.id and character_snapshot_written then return end
        local inventory=collect_traversal_gimmicks(player_pos)
        local state={
            source='ue4ss_traversal_gimmicks',
            schema_version=1,
            mode='traversal_gimmick_snapshot',
            request_id=request.id,
            source_sequence=sequence,
            map_id=map_id,
            player_actor_id=player_id,
            player_position=player_pos,
            timestamp_utc=source_timestamp or os.date('!%Y-%m-%dT%H:%M:%SZ'),
            complete=inventory.complete,
            reason=inventory.reason,
            scan_counts=inventory.scan_counts,
            gimmicks=inventory.gimmicks
        }
        local path=ROOT..'traversal-gimmicks.'..tostring(sequence%2)..'.json'
        character_snapshot_request_id=request.id
        character_snapshot_written=write_json(path,json(state))
        return character_snapshot_written and state.timestamp_utc or nil
    end
    if request.poi_id==CHARACTER_TARGET_POI_ID then
        if character_snapshot_request_id==request.id and character_snapshot_written then return end
        local target=collect_character_target(request.object_id,battle,battle_class,player_pos)
        local state={
            source='ue4ss_character_target',
            schema_version=1,
            mode='targeted_snapshot',
            request_id=request.id,
            source_sequence=sequence,
            map_id=map_id,
            player_actor_id=player_id,
            player_position=player_pos,
            timestamp_utc=source_timestamp or os.date('!%Y-%m-%dT%H:%M:%SZ'),
            complete=target.complete,
            reason=target.reason,
            selector=request.object_id,
            scan_count=target.scan_count,
            target=target.target,
            nearby_radius_m=CHARACTER_TARGET_RADIUS_M,
            nearby=target.nearby,
            lookup=target.lookup
        }
        local path=ROOT..'character-target.'..tostring(sequence%2)..'.json'
        character_snapshot_request_id=request.id
        character_snapshot_written=write_json(path,json(state))
        return character_snapshot_written and state.timestamp_utc or nil
    end
    local key=FName(request.object_id)
    local point=position(safe(function() return field:GetEndTargetPointLocation(key) end))
    local layout=position(safe(function() return battle:GetLayoutTransform(key).Translation end))
    local poi={id=request.poi_id,object_id=request.object_id,point_position=point,layout_position=layout}
    if point and distance(point,{x=0,y=0,z=0})>0.01 then
        poi.position=point;poi.coordinate_source='EndFieldAPI.GetEndTargetPointLocation'
    end
    if #cached_actors==0 or ticks%20==1 then
        cached_actors={}
        for _,entry in ipairs(M.collect()) do
            local path=refs.path(entry.actor)
            if path then cached_actors[#cached_actors+1]={path=path,id=entry.id,kind=entry.kind} end
        end
    end
    local actors={}
    for _,entry in ipairs(cached_actors) do
        local actor=refs.resolve(entry.path)
        if valid(actor) and safe(function() return actor:GetFullName() end,'')==entry.id and actor~=player then
            local is_player=entry.kind=='character' and safe(function() return actor:IsPlayer() end,true)
            if not is_player then
                local p=position(safe(function() return actor:K2_GetActorLocation() end))
                if p and (distance(p,player_pos)<12 or (poi.position and distance(p,poi.position)<15)) then
                    local actor_name=entry.id:match('^(%S+)')
                    if valid(battle_class) and safe(function() return actor:IsA(battle_class) end,false) then
                        actor_name=text(safe(function() return battle:GetActorName(actor) end,actor_name))
                    end
                    actors[#actors+1]={id=entry.id,kind=entry.kind,name=actor_name,position=p,distance=distance(p,player_pos)}
                end
            end
        end
    end
    -- The UI supplies no actor owner. Preserve this explicit conservative
    -- association: one visible command and one non-party entity in range.
    local prompt,button,widget=prompt_reader(true)
    local candidates={}
    for _,actor in ipairs(actors) do if actor.distance<=2.5 then candidates[#candidates+1]=actor end end
    local visible_prompt=prompt and button=='<button=field-action>' and shown(widget)
    if #candidates==1 and visible_prompt then
        candidates[1].prompt=prompt
        candidates[1].prompt_binding='unique_nearby_actor_visible_command'
        last_prompt_target=candidates[1].id
    end
    -- A hidden command is a known negative only for the same previously
    -- associated entity. A missing/unreadable widget remains unknown.
    if last_prompt_target and valid(widget) and shown(widget)==false then
        for _,actor in ipairs(actors) do
            if actor.id==last_prompt_target and actor.distance<=2.5 then actor.prompt='' end
        end
    end
    local state={source='ue4ss_entity_poi',request_id=request.id,source_sequence=sequence,
        map_id=map_id,player_actor_id=player_id,player_position=player_pos,
        collection=scan_info,cached_actor_count=#cached_actors,timestamp_utc=source_timestamp or os.date('!%Y-%m-%dT%H:%M:%SZ'),poi=poi,
        prompt_observation={text=prompt,button=button,candidates=#candidates,visible=visible_prompt and true or false},nearby_entities_complete=false}
    local encoded=json(state)
    encoded=encoded:sub(1,-2)..',"nearby_actors":'..array(actors)..'}'
    local path=ROOT..'entity-poi.'..tostring(sequence%2)..'.json'
    local f=io.open(path,'wb');if f then f:write(encoded);f:flush();f:close() end
    return state.timestamp_utc
end
M.collect=collect
return M

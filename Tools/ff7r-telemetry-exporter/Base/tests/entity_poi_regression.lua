-- Executed in Lua 5.4 with mock game APIs and an in-memory filesystem.
local source=assert(arg[1])
local files,now={},100
local env=setmetatable({}, {__index=_G})
local point={X=1000,Y=0,Z=0}
local registry={}
local function actor(id,x,party,battle)
    id=id:gsub(' World:', ' /World:')
    local value={IsValid=function() return true end,GetFullName=function() return id end,
        IsPlayer=function() return party end,IsA=function() return battle end,
        K2_GetActorRotation=function() return {Pitch=0,Yaw=90,Roll=0} end,
        K2_GetActorLocation=function() return {X=x,Y=0,Z=0} end}
    registry[id:match(" (/.*)$")]=value
    return value
end
local player=actor('PC_Test World:PersistentLevel.Cloud',900,true,true)
local npc=actor('NPC_Test World:PersistentLevel.NPC',1000,false,false)
local party=actor('PC_Party World:PersistentLevel.Party',950,true,true)
local people={npc,party}
local field={GetEndTargetPointLocation=function() return point end}
local battle={GetLayoutTransform=function() return {Translation=point} end,
    GetActorName=function() error('EndCharacter getter called on EndCharacterBase NPC') end}
local class={IsValid=function() return true end}
env.StaticFindObject=function(path)
    if registry[path] then return registry[path] end
    if path:match('Default__EndFieldAPI$') then return field end
    if path:match('Default__EndBattleAPI$') then return battle end
    return class
end
env.require=function(name)
    assert(name=='object_refs')
    return assert(loadfile(assert(arg[2]),'t',env))()
end
env.FName=function(v) return v end
env.FindAllOf=function(name)
    if name=='EndCharacterBase' then return people end
    return {}
end
env.os={time=function() return now end}
env.io={open=function(path,mode)
    if mode=='rb' then
        if not files[path] then return nil end
        return {read=function() return files[path] end,close=function() end}
    end
    return {write=function(_,value) files[path]=value end,flush=function() end,close=function() end}
end}
local root='C:\\CaitGPT\\Telemetry\\'
local visibility=true
local widget={IsValid=function() return true end,GetFullName=function() return 'Command' end,
    IsVisible=function() if visibility=='error' then error('missing') end return visibility end,
    GetRenderOpacity=function() return 1 end,GetParent=function() return nil end}
local function prompt() return 'Talk','<button=field-action>',widget end
local sampler=assert(loadfile(source,'t',env))()
sampler.sample(1,player,'EndGame',prompt,'sample-time')
assert(files[root..'entity-poi.1.json']==nil,'inactive sampler wrote data')
files[root..'entity-poi.request']='one|900|EndGame|point|poi'
for i=2,11 do sampler.sample(i,player,'EndGame',prompt,'sample-time') end
local result=assert(files[root..'entity-poi.1.json'])
assert(result:find('NPC_Test',1,true) and not result:find('PC_Party',1,true))
assert(result:find('"prompt":"Talk"',1,true))
assert(result:find('"timestamp_utc":"sample-time"',1,true))
visibility=false
sampler.sample(12,player,'EndGame',prompt,'sample-time')
assert(files[root..'entity-poi.0.json']:find('"prompt":""',1,true),'hidden command was not recorded')
visibility='error'
sampler.sample(13,player,'EndGame',prompt,'sample-time')
assert(not files[root..'entity-poi.1.json']:find('"prompt":',1,true),'failed visibility became evidence')
visibility=true
people={npc,actor('NPC_Other World:PersistentLevel.Other',1050,false,false)}
for i=14,21 do sampler.sample(i,player,'EndGame',prompt,'sample-time') end
assert(not files[root..'entity-poi.1.json']:find('"prompt":',1,true),'ambiguous actor received prompt')
files[root..'entity-poi.request']='inventory|900|*|point|character_inventory'
people={npc,party}
for i=22,31 do sampler.sample(i,player,'EndGame',prompt,'inventory-time') end
local inventory=assert(files[root..'character-inventory.1.json'])
assert(inventory:find('"source":"ue4ss_character_inventory"',1,true))
assert(inventory:find('NPC_Test',1,true) and inventory:find('PC_Party',1,true))
assert(inventory:find('"complete":true',1,true) and inventory:find('"map_id":"EndGame"',1,true))
assert(inventory:find('"yaw":90',1,true),'inventory omitted the observed facing')
local inventory_first=inventory
for i=32,41 do sampler.sample(i,player,'EndGame',prompt,'inventory-time') end
assert(files[root..'character-inventory.1.json']==inventory_first,'inventory request rescanned after snapshot')
files[root..'entity-poi.request']='candidates|900|*|unused|character_candidates'
for i=42,51 do sampler.sample(i,player,'EndGame',prompt,'candidate-time') end
local candidates=assert(files[root..'character-candidates.1.json'])
assert(candidates:find('"source":"ue4ss_character_candidates"',1,true))
assert(candidates:find('"mode":"candidate_snapshot"',1,true))
assert(not candidates:find('"chara_id"',1,true),'candidate scan exported rich identity fields')
local candidates_first=candidates
for i=52,61 do sampler.sample(i,player,'EndGame',prompt,'candidate-time') end
assert(files[root..'character-candidates.1.json']==candidates_first,'candidate request rescanned after snapshot')
files[root..'entity-poi.request']='target|900|*|NPC_Test /World:PersistentLevel.NPC|character_target'
for i=62,71 do sampler.sample(i,player,'EndGame',prompt,'target-time') end
local target=assert(files[root..'character-target.1.json'])
assert(target:find('"source":"ue4ss_character_target"',1,true))
assert(target:find('"complete":true',1,true) and target:find('NPC_Test /World:PersistentLevel.NPC',1,true))
assert(target:find('"nearby_radius_m":10',1,true))
assert(target:find('"yaw":90',1,true),'target omitted the observed facing')
people={npc}
npc.K2_GetActorRotation=function() error('rotation unavailable') end
files[root..'entity-poi.request']='unknown-rotation|900|*|NPC_Test /World:PersistentLevel.NPC|character_target'
for i=72,81 do sampler.sample(i,player,'EndGame',prompt,'target-time') end
local unknown=assert(files[root..'character-target.1.json'])
assert(unknown:find('"complete":true',1,true))
assert(not unknown:find('"rotation":',1,true),'unreadable rotation fabricated a facing')
-- Field-action actors have no IsPlayer: calling it leaks C stack live.
local bench={IsValid=function() return true end,GetFullName=function() return 'BP_Bench /World:PersistentLevel.Bench' end,
    IsPlayer=function() error('IsPlayer called on a field-action actor') end,
    GetClass=function() return {GetFullName=function() return 'Class /Script/EndGame.EndFieldActionActorBase' end} end,
    K2_GetActorRotation=function() return {Pitch=0,Yaw=0,Roll=0} end,
    K2_GetActorLocation=function() return {X=1100,Y=0,Z=0} end}
local function component(x,y,z,class_name)
    return {IsValid=function() return true end,K2_GetComponentLocation=function() return {X=x,Y=y,Z=z} end,
        GetClass=function() return {GetFName=function() return {ToString=function() return class_name or 'EndFieldActionTriggerComponent' end} end} end}
end
local function gimmick(id,class_name,fields)
    local value={IsValid=function() return true end,GetFullName=function() return id end,
        GetClass=function() return {GetFName=function() return {ToString=function() return class_name end} end} end,
        K2_GetActorRotation=function() return {Pitch=0,Yaw=45,Roll=0} end,
        K2_GetActorLocation=function() return {X=2000,Y=100,Z=50} end}
    for k,v in pairs(fields) do value[k]=v end
    return value
end
local points={component(2000,100,0,'EndFieldClimbingAttachPointSetting'),component(2000,100,400,'EndFieldClimbingFinishPointSetting')}
local ladder=gimmick('BP_Ladder_C /World:PersistentLevel.Ladder','BP_Ladder_C',
    {UpperVolume=component(2000,0,500),LowerVolume=component(2000,0,0),RungCount=12})
local climbing=gimmick('BP_Climb_C /World:PersistentLevel.PipeSide','BP_Climb_C',
    {UpperVolume=component(2000,100,450),
     ClimbingPointArray={ForEach=function(_,f) for i,v in ipairs(points) do if f(i,{get=function() return v end}) then return end end end}})
-- A crack and a crawl space declare EnterVolume/ExitVolume only; reading
-- any other property (UpperVolume) would leak C stack live.
local undeclared={}
local function spline(points)
    return {IsValid=function() return true end,GetNumberOfSplinePoints=function() return #points end,
        GetLocationAtSplinePoint=function(_,index,space)
            assert(space==1,'spline read outside world space')
            local p=points[index+1];return {X=p[1],Y=p[2],Z=p[3]}
        end}
end
local function switch_box(x,y,z)
    local value=component(x,y,z,'EndSwitchAreaBoxComponent')
    value.GetScaledBoxExtent=function() return {X=50,Y=60,Z=70} end
    return value
end
local function passage(id,class_name,spline_field,enter,exit,points)
    local value=gimmick(id,class_name,{EnterVolume=enter,ExitVolume=exit,[spline_field]=spline(points),
        DividedImpassableEnterVolume=switch_box(enter.K2_GetComponentLocation().X,0,0),
        DividedImpassableExitVolume=switch_box(exit.K2_GetComponentLocation().X,0,0)})
    return setmetatable(value,{__index=function(_,key) undeclared[#undeclared+1]=key end})
end
local crack=passage('SampleCrack00_C /World:PersistentLevel.Crack','SampleCrack00_C','CrackComponent',
    component(3000,0,0),component(2400,-600,0),{{3000,0,0},{2700,-300,0},{2400,-600,0}})
local crawl=passage('SampleCrouched00_C /World:PersistentLevel.Crawl','SampleCrouched00_C','CrouchedComponent',
    component(5000,0,0),component(3100,1500,30),{{5000,0,0},{4100,700,10},{3100,1500,30},{2000,2300,30}})
env.FindAllOf=function(name)
    if name=='EndCharacterBase' then return people end
    if name=='EndFieldActionActorBase' then return {bench} end
    if name=='EndFieldActionActorLadder' then return {ladder} end
    if name=='EndFieldActionClimbingActor' then return {climbing} end
    if name=='EndFieldActionActorCrack' then return {crack} end
    if name=='EndFieldActionActorCrouched' then return {crawl} end
    return {}
end
files[root..'entity-poi.request']='actors|900|*|unused|actor_inventory'
for i=82,91 do sampler.sample(i,player,'EndGame',prompt,'actors-time') end
local actors=assert(files[root..'actor-inventory.1.json'],'actor inventory not written')
assert(actors:find('BP_Bench',1,true) and actors:find('"complete":true',1,true))
files[root..'entity-poi.request']='gimmicks|900|*|unused|traversal_gimmicks'
for i=92,101 do sampler.sample(i,player,'EndGame',prompt,'gimmick-time') end
local gimmicks=assert(files[root..'traversal-gimmicks.1.json'],'traversal gimmicks not written')
assert(gimmicks:find('"source":"ue4ss_traversal_gimmicks"',1,true) and gimmicks:find('"complete":true',1,true))
assert(gimmicks:find('"kind":"ladder"',1,true) and gimmicks:find('"rung_count":12',1,true))
assert(gimmicks:find('"lower":{',1,true),'ladder lower end missing')
assert(gimmicks:find('"kind":"climbing"',1,true) and gimmicks:find('EndFieldClimbingFinishPointSetting',1,true))
assert(gimmicks:find('"z":400',1,true),'climbing finish point position missing')
assert(not gimmicks:find('BP_Bench',1,true),'field-action actor leaked into gimmick scan')
assert(gimmicks:find('"kind":"crack"',1,true) and gimmicks:find('"kind":"crouch"',1,true),'passages missing')
assert(gimmicks:find('"enter":{',1,true) and gimmicks:find('"x":3000',1,true),'crack enter volume missing')
assert(select(2,gimmicks:gsub('"exit":{',''))==2 and select(2,gimmicks:gsub('"enter":{',''))==2,'passage enter/exit volumes missing')
assert(gimmicks:find('"spline":[',1,true) and gimmicks:find('"x":4100',1,true) and gimmicks:find('"x":2700',1,true),'passage spline points missing')
assert(gimmicks:find('"impassable_exit":{',1,true) and gimmicks:find('"extent":{',1,true),'passage switch boxes missing')
assert(#undeclared==0,'undeclared passage property read: '..tostring(undeclared[1]))
now=901
local previous=files[root..'entity-poi.0.json']
sampler.sample(22,player,'EndGame',prompt,'later')
assert(files[root..'entity-poi.0.json']==previous,'expired request sampled')
print('PASS: base-class NPC, party exclusion, type-safe getter, visibility, ambiguity, actor inventory, traversal gimmicks, expiry, exact timestamp')

"""Synthetic Lua mocks only; no live selection or save authorization proof."""
from pathlib import Path
import pytest
LuaRuntime = pytest.importorskip("lupa.lua54").LuaRuntime
SOURCE = Path(__file__).parents[2] / "ff7r-telemetry-exporter/Base/Scripts/map_query.lua"
HARNESS = r"""
function array() return {} end
function safe(fn, default) local ok,v=pcall(fn); if ok then return v else return default end end
function valid(v) return type(v)=='table' and v.valid ~= false end
function text(v) if v==nil then return nil end return tostring(v) end
function visible(v) return v.visible ~= false end
function reflected_property_types(v) return {} end
SAFE_PROPERTY_TYPES={}
MAX_TREE_DEPTH=4
function node_kind(n) return n.kind or 'other',n.class or 'EndWidget' end
function geometry_of() return nil end
function StaticFindObject() return nil end
objects={}
function FindAllOf(name) return objects[name] end
function node(name,kind,children)
 local n={name=name,kind=kind,children=children or {}}
 function n:GetFullName() return 'Widget World.'..self.name end
 function n:GetRenderOpacity() return 1 end
 function n:GetText() return self.name end
 function n:GetChildrenCount() return #self.children end
 function n:GetChildAt(i) return self.children[i+1] end
 return n
end
function row(name)
 local r=node(name,'user');r.WidgetTree={RootWidget=node('Root','panel',{node('Txt_ListNum','text')})}
 return r
end
function list(name,selected,child)
 local l=node(name);l.selected=selected;l.child=child
 function l:GetFullName() return 'EndListBox /Temp/World.'..self.name end
 function l:GetSelectedIndex(user) assert(user==0);return self.selected end
 function l:GetChildAt(i) assert(i==self.selected);return self.child end
 function l:GetCursorWidget() return node('Cursor') end
 function l:GetClass() return {GetFullName=function() return 'Class EndListBox' end} end
 function l:GetOuter() return nil end
 return l
end
"""
def runtime():
 lua=LuaRuntime(unpack_returned_tuples=True)
 lua.execute(HARNESS)
 source=SOURCE.read_text(encoding="utf-8")
 block=source[source.index("local MAX_SAVE_CELLS"):source.index("-- Read-only confirmation evidence")]
 api=lua.execute(block + "\nreturn {walk=cell_nodes,cells=save_cells}")
 return lua,api


def test_selected_child_exactly_links_row_without_index_arithmetic():
 lua,api=runtime()
 lua.execute("active=row('SaveData15');prototype=row('Old02');unrelated=node('Other');objects.SaveData_Cell_C={prototype,active};objects.EndListBox={list('Wrong',0,unrelated),list('SaveList',7,active)}")
 result=lua.table()
 cells=api.cells(result)
 assert len(cells)==2
 assert result.save_lists[2].selected_save_cell_id is None
 assert result.save_lists[1].selected_save_cell_id==cells[2].id
 assert result.save_lists[1].selected_index==7
 assert result.save_lists[1].selected_child_id==cells[2].full_id
 assert result.save_list is None  # Never a guessed Package outer.


def test_invalid_selected_child_does_not_bind_prototype():
 lua,api=runtime()
 lua.execute("prototype=row('Old02');objects.SaveData_Cell_C={prototype};objects.EndListBox={list('SaveList',-1,nil)}")
 result=lua.table();api.cells(result)
 assert result.save_lists[1].selected_save_cell_id is None


def test_title_selection_is_read_without_player_or_save_cells_and_requires_sdk_owner():
 lua,api=runtime()
 lua.execute("""
 title_class={}
 function StaticFindObject(path) if path=='/Script/EndGame.EndStartMenu' then return title_class end end
 owner=node('TitleOwner')
 function owner:IsA(cls) assert(cls==title_class);return true end
 function owner:GetOuter() return nil end
 selected=node('TitleEntry','panel',{node('$menu_title_0001_0002','text')})
 active=list('TitleList',2,selected)
 function active:GetOuter() return owner end
 unrelated=list('OtherList',0,node('Other'))
 objects.EndListBox={active,unrelated};objects.EndVirtualScrollBox={}
 -- No SaveData instances exist before Load Game is opened.
 objects.SaveData_Cell_C=nil
 """)
 result=lua.table();cells=api.cells(result,True)
 assert len(cells)==0 and len(result.title_lists)==1
 title=result.title_lists[1]
 assert title.title_owner_id=='Widget World.TitleOwner' and title.title_owner_visible is True
 assert title.selected_child_id=='Widget World.TitleEntry'
 assert title.selected_nodes[2].text=='$menu_title_0001_0002'
 assert title.selected_node_walk.truncated is False
 assert result.save_list_discovery.scan_truncated is False
 lua.execute("function owner:IsA() return false end")
 result=lua.table();api.cells(result,True)
 assert len(result.title_lists)==0


def test_title_portrait_list_without_selection_does_not_ambiguate_menu():
 lua,api=runtime()
 lua.execute("""
 title_class={}
 function StaticFindObject(path) if path=='/Script/EndGame.EndStartMenu' then return title_class end end
 owner=node('TitleOwner')
 function owner:IsA() return true end
 function owner:GetOuter() return nil end
 active=list('List_Menu',1,node('Continue','panel',{node('$menu_title_0001_0001','text')}))
 portrait=list('CharaFace',-1,nil)
 function active:GetOuter() return owner end
 function portrait:GetOuter() return owner end
 objects.EndListBox={};objects.EndVirtualScrollBox={active,portrait}
 """)
 result=lua.table();api.cells(result,True)
 assert len(result.title_lists)==1
 assert result.title_lists[1].id.endswith('List_Menu')
 assert len(result.save_lists)==2  # Preserve the non-selected list for diagnostics.
 assert result.save_list_discovery.candidates==2
 # A second genuine selection still reaches the reader's ambiguity guard.
 lua.execute("portrait.selected=0;portrait.child=node('OtherChoice','text')")
 result=lua.table();api.cells(result,True)
 assert len(result.title_lists)==2


def test_title_selected_trees_respect_the_record_cap():
 lua,api=runtime()
 lua.execute("""
 title_class={}
 function StaticFindObject(path) if path=='/Script/EndGame.EndStartMenu' then return title_class end end
 owner=node('TitleOwner')
 function owner:IsA() return true end
 function owner:GetOuter() return nil end
 objects.EndListBox={};objects.EndVirtualScrollBox={}
 for i=1,26 do
  local entry=list('TitleList'..i,0,node('TitleEntry'..i,'text'))
  function entry:GetOuter() return owner end
  table.insert(objects.EndListBox,entry)
 end
 """)
 result=lua.table();api.cells(result,True)
 assert len(result.title_lists)==24 and result.save_list_discovery.records_truncated is True


def test_complete_walk_keeps_essential_nodes_after_old48_limit():
 lua,api=runtime()
 lua.execute("children={};for i=1,55 do children[i]=node('Text'..i,'text') end;widget=row('Row');widget.WidgetTree.RootWidget=node('Root','panel',children)")
 nodes,meta=api.walk(lua.globals().widget)
 assert len(nodes)==56 and nodes[56].name=='Text55'
 assert meta.truncated is False and meta.visited==56


def test_node_cap_is_explicit_and_bounded():
 lua,api=runtime()
 lua.execute("branches={};for j=1,3 do local children={};for i=1,60 do children[i]=node('Text'..i,'text') end;branches[j]=node('Branch'..j,'panel',children) end;widget=row('Row');widget.WidgetTree.RootWidget=node('Root','panel',branches)")
 nodes,meta=api.walk(lua.globals().widget)
 assert len(nodes)==128 and meta.truncated is True and meta.node_limit==128


def test_child_cap_is_explicit_even_when_node_cap_not_reached():
 lua,api=runtime()
 lua.execute("children={};for i=1,80 do children[i]=node('Text'..i,'text') end;widget=row('Row');widget.WidgetTree.RootWidget=node('Root','panel',children)")
 nodes,meta=api.walk(lua.globals().widget)
 assert len(nodes)==65 and meta.truncated is True



def test_generic_tree_panel_walk_remains_independent_of_save_local_constants():
 lua=LuaRuntime(unpack_returned_tuples=True)
 lua.execute(HARNESS)
 lua.execute("MAX_TREE_NODES=24;MAX_IMAGES=6;MAX_TEXTS=6;function dtrace() end")
 source=SOURCE.read_text(encoding="utf-8")
 block=source[source.index("local function describe_tree"):source.index("-- Live 2026-09-26 reading a candidate property")]
 walk=lua.execute(block+"\nreturn describe_tree")
 lua.execute("widget=row('Generic');widget.WidgetTree.RootWidget=node('Root','panel',{node('VisibleText','text')})")
 images,texts=walk(lua.globals().widget)
 assert len(images)==0 and len(texts)==1 and texts[1]=='VisibleText'
 assert lua.globals().truncated is None



def test_blank_object_identity_never_links_selected_child_to_anonymous_row():
 lua,api=runtime()
 lua.execute("anonymous=row('Missing');function anonymous:GetFullName() return '' end;objects.SaveData_Cell_C={anonymous};objects.EndListBox={list('SaveList',0,anonymous)}")
 result=lua.table();cells=api.cells(result)
 assert len(cells)==0 and result.save_lists[1].selected_child_id is None
 assert result.save_lists[1].selected_save_cell_id is None


def test_unreadable_panel_count_cannot_claim_complete_walk():
 lua,api=runtime()
 lua.execute("widget=row('Row');function widget.WidgetTree.RootWidget:GetChildrenCount() error('unreadable') end")
 nodes,meta=api.walk(lua.globals().widget)
 assert len(nodes)==1 and meta.truncated is True


def test_missing_root_or_child_cannot_claim_complete_walk():
 lua,api=runtime()
 lua.execute("widget=row('Row');widget.WidgetTree.RootWidget=nil")
 nodes,meta=api.walk(lua.globals().widget)
 assert len(nodes)==0 and meta.truncated is True
 lua.execute("widget=row('Row');function widget.WidgetTree.RootWidget:GetChildAt(i) return nil end")
 nodes,meta=api.walk(lua.globals().widget)
 assert len(nodes)==1 and meta.truncated is True


def test_asset_prefix_cannot_exhaust_records_before_live_save_list():
 lua,api=runtime()
 lua.execute("active=row('Save15');objects.SaveData_Cell_C={active};objects.EndListBox={};for i=1,80 do local x=list('Asset'..i,0,nil);function x:GetFullName() return 'EndListBox /Game/Prototype.'..self.name end;objects.EndListBox[i]=x end;objects.EndListBox[81]=list('ActualSave',14,active)")
 result=lua.table();cells=api.cells(result)
 assert len(result.save_lists)==1 and result.save_lists[1].selected_child_id==cells[1].full_id
 assert result.save_list_discovery.scanned==81 and result.save_list_discovery.prototypes_skipped==80
 assert result.save_list_discovery.scan_truncated is False


def test_whole_discovery_bound_is_explicit():
 lua,api=runtime()
 lua.execute("objects.SaveData_Cell_C={};objects.EndListBox={};for i=1,2100 do local x=list('Asset'..i,0,nil);function x:GetFullName() return 'EndListBox /Game/Prototype.'..self.name end;objects.EndListBox[i]=x end")
 result=lua.table();api.cells(result)
 assert result.save_list_discovery.scanned==2048 and result.save_list_discovery.scan_truncated is True
 assert len(result.save_lists)==0


def test_exact_save_child_priority_survives_other_runtime_list_prefix():
 lua,api=runtime()
 lua.execute("active=row('Save15');objects.SaveData_Cell_C={active};objects.EndListBox={};for i=1,40 do objects.EndListBox[i]=list('Other'..i,0,node('OtherChild')) end;objects.EndListBox[41]=list('ActualSave',14,active)")
 result=lua.table();cells=api.cells(result)
 assert len(result.save_lists)==24 and result.save_lists[1].selected_child_id==cells[1].full_id
 assert result.save_list_discovery.records_truncated is True


def test_ue4ss_object_userdata_is_not_filtered_as_non_table():
 lua,api=runtime()
 active=lua.eval("row('Save15')")
 class ObjectMock:
  visible = True
  def GetFullName(self,*args): return 'EndListBox /Temp/World.ActualSave'
  def GetSelectedIndex(self,*args): return 14
  def GetChildAt(self,*args): return active
  def GetCursorWidget(self,*args): return None
  def GetClass(self,*args): return lua.eval("{GetFullName=function() return 'Class EndListBox' end}")
  def GetOuter(self,*args): return None
 obj=ObjectMock()
 lua.globals().objects.SaveData_Cell_C=lua.table_from([active])
 lua.globals().objects.EndListBox=lua.table_from([obj])
 lua.execute("function valid(v) return type(v)=='userdata' or (type(v)=='table' and v.valid~=false) end")
 result=lua.table();cells=api.cells(result)
 assert result.save_lists[1].selected_child_id==cells[1].full_id


def test_virtual_scroll_exact_link_after_both_asset_prefixes():
 lua,api=runtime()
 lua.execute("active=row('Save15');objects.SaveData_Cell_C={active};objects.EndListBox={};objects.EndVirtualScrollBox={};for i=1,40 do local x=list('Asset'..i,0,nil);function x:GetFullName() return 'EndListBox /Game/Prototype.'..self.name end;objects.EndListBox[i]=x;objects.EndVirtualScrollBox[i]=x end;objects.EndVirtualScrollBox[41]=list('ActualSave',14,active)")
 result=lua.table();cells=api.cells(result)
 assert len(result.save_lists)==1 and result.save_lists[1].selected_child_id==cells[1].full_id
 assert result.save_lists[1].discovery_class=='EndVirtualScrollBox'
 assert result.save_list_discovery.scanned==81
 assert result.save_list_discovery.classes[2].scanned==41


def test_runtime_full_id_deduplicated_across_classes():
 lua,api=runtime()
 lua.execute("active=row('Save15');objects.SaveData_Cell_C={active};shared=list('ActualSave',14,active);objects.EndListBox={shared};objects.EndVirtualScrollBox={shared}")
 result=lua.table();api.cells(result)
 assert len(result.save_lists)==1 and result.save_list_discovery.duplicates_skipped==1
 assert result.save_list_discovery.matched_cells==1 and result.save_list_discovery.scanned==2


def test_scan_bound_shared_across_both_classes():
 lua,api=runtime()
 lua.execute("objects.SaveData_Cell_C={};objects.EndListBox={};objects.EndVirtualScrollBox={};for i=1,1300 do local x=list('Asset'..i,0,nil);function x:GetFullName() return 'EndListBox /Game/Prototype.'..self.name end;objects.EndListBox[i]=x;objects.EndVirtualScrollBox[i]=x end")
 result=lua.table();api.cells(result)
 assert result.save_list_discovery.scanned==2048 and result.save_list_discovery.scan_truncated is True
 assert result.save_list_discovery.classes[1].scanned==1300
 assert result.save_list_discovery.classes[2].scanned==748


def test_record_bound_shared_and_virtual_match_prioritized():
 lua,api=runtime()
 lua.execute("active=row('Save15');objects.SaveData_Cell_C={active};objects.EndListBox={};objects.EndVirtualScrollBox={list('ActualSave',14,active)};for i=1,40 do objects.EndListBox[i]=list('Other'..i,0,node('OtherChild')) end")
 result=lua.table();cells=api.cells(result)
 assert len(result.save_lists)==24 and result.save_lists[1].selected_child_id==cells[1].full_id
 assert result.save_list_discovery.records_truncated is True


def test_deep_prompt_walk_exports_text_and_exact_parent_links():
 lua,api=runtime()
 lua.execute("tail=node('No','text');for i=8,1,-1 do tail=node('Panel'..i,'panel',{tail}) end;widget=row('Prompt');widget.WidgetTree.RootWidget=tail")
 nodes,meta=api.walk(lua.globals().widget)
 assert meta.truncated is False and meta.depth_limit==12
 assert nodes[9].text=='No' and nodes[9].depth==8
 assert nodes[9].full_id=='Widget World.No'
 assert nodes[9].parent_full_id=='Widget World.Panel8'


def test_dedicated_prompt_depth_budget_still_refuses_complete_claim():
 lua,api=runtime()
 lua.execute("tail=node('HiddenTail','text');for i=20,1,-1 do tail=node('Panel'..i,'panel',{tail}) end;widget=row('Prompt');widget.WidgetTree.RootWidget=tail")
 nodes,meta=api.walk(lua.globals().widget)
 assert len(nodes)==13 and meta.truncated is True and meta.depth_limit==12
 assert all(nodes[i].name!='HiddenTail' for i in range(1,len(nodes)+1))


def test_focused_child_getter_is_class_gated_and_exact():
 lua,api=runtime()
 lua.execute("cls={};function StaticFindObject(path) assert(path=='/Script/EndGame.EndUserWidget');return cls end;child=node('ActualFocused');user=row('Button');function user:IsA(c) return c==cls end;function user:GetFocusedChildWidget() return child end;widget=row('Prompt');widget.WidgetTree.RootWidget=user")
 nodes,meta=api.walk(lua.globals().widget)
 assert nodes[1].focused_child_id=='Widget World.ActualFocused'
 lua.execute("function user:IsA(c) return false end;function user:GetFocusedChildWidget() error('must not call') end")
 nodes,meta=api.walk(lua.globals().widget)
 assert nodes[1].focused_child_id is None
 lua.execute("function user:IsA(c) return true end;function child:GetFullName() return '' end;function user:GetFocusedChildWidget() return child end")
 nodes,meta=api.walk(lua.globals().widget)
 assert nodes[1].focused_child_id is None


def test_focus_diagnostics_distinguish_no_child_failure_and_false():
 lua,api=runtime()
 lua.execute("cls={};function StaticFindObject(path) return cls end;user=row('Button');function user:IsA(c) return true end;function user:GetFocusedChildWidget() return nil end;function user:HasAnyUserFocus() return false end;function user:HasFocusedDescendants() return true end;widget=row('Prompt');widget.WidgetTree.RootWidget=user")
 nodes,_=api.walk(lua.globals().widget)
 assert nodes[1].focused_child_status=='no_valid_child'
 assert nodes[1].focus_diagnostics.any_user_focus is False
 assert nodes[1].focus_diagnostics.focused_descendants is True
 assert nodes[1].focus_diagnostics.any_user_focus_status=='read'
 lua.execute("function user:GetFocusedChildWidget() error('unavailable') end;function user:HasAnyUserFocus() error('unavailable') end")
 nodes,_=api.walk(lua.globals().widget)
 assert nodes[1].focused_child_status=='getter_failed'
 assert nodes[1].focus_diagnostics.any_user_focus is None
 assert nodes[1].focus_diagnostics.any_user_focus_status=='getter_failed'
 lua.execute("function user:IsA(c) return false end;function user:GetFocusedChildWidget() error('must not call') end")
 nodes,_=api.walk(lua.globals().widget)
 assert nodes[1].focused_child_status=='not_end_user_widget'

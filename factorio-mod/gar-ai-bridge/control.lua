local PROTOCOL_VERSION = "1.0"
local BRIDGE_VERSION = "0.1.0"
local MAX_SEEN = 512

-- Diagnostics are surfaced through the `ping` response and the Factorio log so a
-- controller can tell "the mod never took the packet" apart from "the mod took it
-- and failed". recv_udp_ok/recv_udp_err count the polling loop itself, while
-- packets_* count what actually reached the mod's UDP event handler.
local DIAG_LOG = true

local function init_state()
  storage.gar_ai_bridge = storage.gar_ai_bridge or {
    seen = {},
    seen_order = {},
    pending_moves = {},
    last_research = {tick = 0, progress = 0},
    diag = {
      packets_seen = 0,
      packets_bad_json = 0,
      packets_bad_proto = 0,
      packets_handled = 0,
      handler_errors = 0,
      recv_calls = 0,
      recv_ack = 0,
      recv_errors = 0,
      last_error = nil,
      last_packet_tick = nil,
      init_tick = nil,
    },
  }
  local diag = storage.gar_ai_bridge.diag
  if diag and diag.init_tick == nil then diag.init_tick = game.tick end
end

local function diag()
  init_state()
  return storage.gar_ai_bridge.diag
end

local function diag_log(message)
  if DIAG_LOG then log("[gar-ai-bridge] " .. tostring(message)) end
end

script.on_init(init_state)
script.on_configuration_changed(init_state)

local function remember_operation(operation_id, response)
  if not operation_id then return end
  local state = storage.gar_ai_bridge
  if not state.seen[operation_id] then
    table.insert(state.seen_order, operation_id)
  end
  state.seen[operation_id] = response
  while #state.seen_order > MAX_SEEN do
    local oldest = table.remove(state.seen_order, 1)
    state.seen[oldest] = nil
  end
end

local function send_response(port, request, accepted, result, err)
  local response = {
    protocol_version = PROTOCOL_VERSION,
    bridge_version = BRIDGE_VERSION,
    request_id = request.request_id,
    operation_id = request.operation_id,
    type = "response",
    accepted = accepted == true,
    game_tick = game.tick,
    result = result or {},
    error = err,
  }
  remember_operation(request.operation_id, response)
  helpers.send_udp(port, helpers.table_to_json(response))
end

local function reply_cached(port, operation_id)
  if not operation_id then return false end
  local cached = storage.gar_ai_bridge.seen[operation_id]
  if cached then
    helpers.send_udp(port, helpers.table_to_json(cached))
    return true
  end
  return false
end

local function choose_player(request, event)
  local payload = request.payload or {}
  local index = tonumber(payload.player_index)
  if not index and event.player_index and event.player_index > 0 then
    index = event.player_index
  end
  if index then
    local player = game.get_player(index)
    if player and player.valid then return player end
  end
  for _, player in pairs(game.connected_players) do
    if player and player.valid then return player end
  end
  return game.get_player(1)
end

local function pos_array(position)
  return {position.x, position.y}
end

local function aggregate_contents(inventory)
  local out = {}
  if not inventory or not inventory.valid then return out end
  for _, stack in pairs(inventory.get_contents()) do
    out[stack.name] = (out[stack.name] or 0) + stack.count
  end
  return out
end

local function merge_counts(target, source)
  for name, count in pairs(source) do
    target[name] = (target[name] or 0) + count
  end
end

local function entity_inventory(entity)
  local out = {}
  local max_index = entity.get_max_inventory_index and entity.get_max_inventory_index() or 0
  for index = 1, max_index do
    local ok, inv = pcall(function() return entity.get_inventory(index) end)
    if ok and inv and inv.valid then
      merge_counts(out, aggregate_contents(inv))
    end
  end
  local fuel = entity.get_fuel_inventory and entity.get_fuel_inventory() or nil
  if fuel and fuel.valid then merge_counts(out, aggregate_contents(fuel)) end
  local output = entity.get_output_inventory and entity.get_output_inventory() or nil
  if output and output.valid then merge_counts(out, aggregate_contents(output)) end
  return out
end

local function entity_recipe(entity)
  if not entity.get_recipe then return nil end
  local ok, recipe = pcall(function() return entity.get_recipe() end)
  if not ok or not recipe then return nil end
  return recipe.name
end

local function serialize_entity(entity)
  return {
    name = entity.name,
    type = entity.type,
    position = pos_array(entity.position),
    direction = entity.direction,
    unit_number = entity.unit_number,
    force = entity.force and entity.force.name or nil,
    health = entity.health,
    active = entity.active,
    status = entity.status,
    inventory = entity_inventory(entity),
    recipe = entity_recipe(entity),
  }
end

local function nearby_entities(player, radius)
  local p = player.position
  local entities = player.surface.find_entities_filtered{
    area = {{p.x - radius, p.y - radius}, {p.x + radius, p.y + radius}},
  }
  local result = {}
  for _, entity in pairs(entities) do
    if entity.valid and entity.name ~= "character" then
      table.insert(result, serialize_entity(entity))
    end
  end
  return result
end

local function science_supply(player)
  local out = {}
  local p = player.position
  local labs = player.surface.find_entities_filtered{
    type = "lab",
    force = player.force,
    area = {{p.x - 256, p.y - 256}, {p.x + 256, p.y + 256}},
  }
  for _, lab in pairs(labs) do
    merge_counts(out, entity_inventory(lab))
  end
  return out
end

local function research_state(player)
  local force = player.force
  local current = force.current_research
  if not current then
    return {
      technology = nil,
      progress = 0,
      unit_count = nil,
      science_supply = {},
      state = "idle",
    }
  end
  local progress = force.research_progress or 0
  local supply = science_supply(player)
  local has_science = false
  for name, count in pairs(supply) do
    if string.find(name, "science%-pack") and count > 0 then
      has_science = true
      break
    end
  end
  local state = "starved"
  local last = storage.gar_ai_bridge.last_research or {tick = 0, progress = 0}
  if progress > (last.progress or 0) or has_science then state = "progressing" end
  storage.gar_ai_bridge.last_research = {tick = game.tick, progress = progress}
  local unit_count = nil
  if current.prototype and current.prototype.research_unit_count_formula then
    unit_count = current.prototype.research_unit_count_formula
  end
  return {
    technology = current.name,
    progress = progress,
    unit_count = unit_count,
    science_supply = supply,
    state = state,
  }
end

local function snapshot(player)
  local inventory = player.get_main_inventory()
  return {
    game_tick = game.tick,
    game_version = script.active_mods and script.active_mods.base or nil,
    bridge_version = BRIDGE_VERSION,
    player = {
      index = player.index,
      position = pos_array(player.position),
      surface = player.surface.name,
      force = player.force.name,
      inventory = aggregate_contents(inventory),
      health = player.character and player.character.valid and player.character.health or nil,
      crafting_queue_size = player.crafting_queue_size,
    },
    entities = nearby_entities(player, 48),
    power = {margin = nil, status = "unknown"},
    resources = {
      iron = {stock = inventory and inventory.get_item_count("iron-plate") or 0, rate = nil},
      copper = {stock = inventory and inventory.get_item_count("copper-plate") or 0, rate = nil},
      coal = {stock = inventory and inventory.get_item_count("coal") or 0, rate = nil},
    },
    research = research_state(player),
    threat = {
      level = player.surface.find_nearest_enemy{
        position = player.position,
        max_distance = 64,
        force = player.force,
      } and "nearby" or "low",
    },
  }
end

local function query_recipe(player, name)
  local recipe = player.force.recipes[name]
  if not recipe then return nil end
  local ingredients = {}
  for _, ingredient in pairs(recipe.ingredients or {}) do
    table.insert(ingredients, {
      name = ingredient.name,
      type = ingredient.type,
      amount = ingredient.amount,
    })
  end
  local products = {}
  for _, product in pairs(recipe.products or {}) do
    table.insert(products, {
      name = product.name,
      type = product.type,
      amount = product.amount,
      amount_min = product.amount_min,
      amount_max = product.amount_max,
      probability = product.probability,
    })
  end
  return {
    name = recipe.name,
    enabled = recipe.enabled,
    category = recipe.category,
    energy = recipe.energy,
    ingredients = ingredients,
    products = products,
  }
end

local function query_technology(player, name)
  local tech = player.force.technologies[name]
  if not tech then return nil end
  local prerequisites = {}
  for prereq_name, _ in pairs(tech.prerequisites or {}) do
    table.insert(prerequisites, prereq_name)
  end
  table.sort(prerequisites)
  return {
    name = tech.name,
    enabled = tech.enabled,
    researched = tech.researched,
    level = tech.level,
    prerequisites = prerequisites,
  }
end

local function find_target(player, x, y, radius)
  local entities = player.surface.find_entities_filtered{
    position = {x, y},
    radius = radius or 0.35,
  }
  for _, entity in pairs(entities) do
    if entity.valid and entity.force == player.force and entity.name ~= "character" then
      return entity
    end
  end
  return nil
end

local function action_move_to(port, request, player, params)
  local x, y = tonumber(params.x), tonumber(params.y)
  if not x or not y then
    send_response(port, request, false, nil, "move_to requires numeric x/y")
    return
  end
  local ok = player.teleport({x, y})
  send_response(port, request, ok == true, {position = pos_array(player.position)}, ok and nil or "teleport failed")
end

local function action_ensure_item(port, request, player, params)
  local item = tostring(params.item or "")
  local target = tonumber(params.count)
  if item == "" or not target or target < 0 then
    send_response(port, request, false, nil, "ensure_item requires item and count >= 0")
    return
  end
  local inventory = player.get_main_inventory()
  local current = inventory and inventory.get_item_count(item) or 0
  if current >= target then
    send_response(port, request, true, {count = current, already_satisfied = true})
    return
  end
  local recipe = player.force.recipes[item]
  if not recipe or not recipe.enabled then
    send_response(port, request, false, {count = current}, "no enabled hand-craft recipe for item")
    return
  end
  local needed = target - current
  local craftable = player.get_craftable_count(recipe)
  if craftable <= 0 then
    send_response(port, request, false, {count = current}, "missing hand-crafting ingredients")
    return
  end
  local started = player.begin_crafting{count = math.min(needed, craftable), recipe = recipe, silent = true}
  send_response(port, request, started > 0, {count = current, crafts_started = started}, started > 0 and nil or "crafting did not start")
end

local function action_place_entity(port, request, player, params)
  local name = tostring(params.name or "")
  local x, y = tonumber(params.x), tonumber(params.y)
  local direction = tonumber(params.direction) or defines.direction.north
  if name == "" or not x or not y then
    send_response(port, request, false, nil, "place_entity requires name/x/y")
    return
  end
  local existing = player.surface.find_entity(name, {x, y})
  if existing and existing.valid then
    send_response(port, request, true, {entity = serialize_entity(existing), already_satisfied = true})
    return
  end
  local inventory = player.get_main_inventory()
  if not inventory or inventory.get_item_count(name) <= 0 then
    send_response(port, request, false, nil, "player inventory does not contain entity item")
    return
  end
  if not player.can_place_entity{name = name, position = {x, y}, direction = direction} then
    send_response(port, request, false, nil, "cannot place entity at target")
    return
  end
  local removed = inventory.remove{name = name, count = 1}
  if removed ~= 1 then
    send_response(port, request, false, nil, "failed to consume entity item")
    return
  end
  local entity = player.surface.create_entity{
    name = name,
    position = {x, y},
    direction = direction,
    force = player.force,
    player = player,
    raise_built = true,
    create_build_effect_smoke = true,
  }
  if not entity then
    inventory.insert{name = name, count = 1}
    send_response(port, request, false, nil, "entity creation failed; item refunded")
    return
  end
  send_response(port, request, true, {entity = serialize_entity(entity)})
end

local function action_transfer(port, request, player, params)
  local item = tostring(params.item or "")
  local count = tonumber(params.count)
  local x, y = tonumber(params.x), tonumber(params.y)
  if item == "" or not count or count < 0 or not x or not y then
    send_response(port, request, false, nil, "transfer requires item/count/x/y")
    return
  end
  if count == 0 then
    send_response(port, request, true, {moved = 0, already_satisfied = true})
    return
  end
  local target = find_target(player, x, y, 0.5)
  if not target then
    send_response(port, request, false, nil, "target entity missing")
    return
  end
  local player_inventory = player.get_main_inventory()
  local available = player_inventory and player_inventory.get_item_count(item) or 0
  if available <= 0 then
    send_response(port, request, false, nil, "no source items")
    return
  end
  local to_move = math.min(count, available)
  local removed = player_inventory.remove{name = item, count = to_move}
  if removed <= 0 then
    send_response(port, request, false, nil, "failed to remove source items")
    return
  end
  local inserted = target.insert{name = item, count = removed}
  if inserted < removed then
    player_inventory.insert{name = item, count = removed - inserted}
  end
  send_response(port, request, inserted > 0, {moved = inserted}, inserted > 0 and nil or "target rejected item")
end

local function action_set_recipe(port, request, player, params)
  local x, y = tonumber(params.x), tonumber(params.y)
  local recipe_name = tostring(params.recipe or "")
  if not x or not y or recipe_name == "" then
    send_response(port, request, false, nil, "set_recipe requires x/y/recipe")
    return
  end
  local entity = find_target(player, x, y, 0.5)
  if not entity then
    send_response(port, request, false, nil, "target entity missing")
    return
  end
  local recipe = player.force.recipes[recipe_name]
  if not recipe or not recipe.enabled then
    send_response(port, request, false, nil, "recipe missing or disabled")
    return
  end
  local ok, err = pcall(function() entity.set_recipe(recipe) end)
  if not ok then
    send_response(port, request, false, nil, tostring(err))
    return
  end
  send_response(port, request, true, {recipe = entity_recipe(entity)})
end

local function action_start_research(port, request, player, params)
  local name = tostring(params.technology or "")
  if name == "" then
    send_response(port, request, false, nil, "start_research requires technology")
    return
  end
  local tech = player.force.technologies[name]
  if not tech or not tech.enabled or tech.researched then
    send_response(port, request, false, nil, "technology unavailable")
    return
  end
  local ok = player.force.add_research(name)
  send_response(port, request, ok == true, {technology = name}, ok and nil or "research could not be queued")
end

local ACTIONS = {
  move_to = action_move_to,
  ensure_item = action_ensure_item,
  place_entity = action_place_entity,
  transfer = action_transfer,
  set_recipe = action_set_recipe,
  start_research = action_start_research,
}

local function handle_request(event)
  init_state()
  local d = diag()
  d.packets_seen = d.packets_seen + 1
  d.last_packet_tick = game.tick

  local ok, request = pcall(helpers.json_to_table, event.payload)
  if not ok or type(request) ~= "table" then
    d.packets_bad_json = d.packets_bad_json + 1
    diag_log("packet " .. d.packets_seen .. " rejected: payload is not a JSON object")
    return
  end
  local port = event.source_port
  if request.protocol_version ~= PROTOCOL_VERSION then
    d.packets_bad_proto = d.packets_bad_proto + 1
    diag_log("packet " .. d.packets_seen .. " rejected: protocol " .. tostring(request.protocol_version))
    send_response(port, request, false, nil, "protocol_version mismatch")
    return
  end
  if reply_cached(port, request.operation_id) then
    d.packets_handled = d.packets_handled + 1
    return
  end

  -- The whole dispatch runs under pcall: an unexpected error inside one op must
  -- not abort the tick handler that polls helpers.recv_udp().
  local handled, err = pcall(function()
    local player = choose_player(request, event)
    if not player or not player.valid then
      send_response(port, request, false, nil, "no valid player available")
      return
    end

    local op = request.op
    local payload = request.payload or {}
    if op == "ping" then
      send_response(port, request, true, {
        game_tick = game.tick,
        bridge_version = BRIDGE_VERSION,
        base_version = script.active_mods.base,
        mod_version = script.active_mods["gar-ai-bridge"],
        diag = {
          packets_seen = d.packets_seen,
          packets_handled = d.packets_handled,
          packets_bad_json = d.packets_bad_json,
          packets_bad_proto = d.packets_bad_proto,
          handler_errors = d.handler_errors,
          recv_calls = d.recv_calls,
          recv_ack = d.recv_ack,
          recv_errors = d.recv_errors,
          last_error = d.last_error,
          last_packet_tick = d.last_packet_tick,
          init_tick = d.init_tick,
        },
      })
    elseif op == "snapshot" then
      send_response(port, request, true, snapshot(player))
    elseif op == "scan_area" then
      local center = payload.center or {player.position.x, player.position.y}
      local radius = tonumber(payload.radius) or 16
      local entities = player.surface.find_entities_filtered{
        area = {{center[1] - radius, center[2] - radius}, {center[1] + radius, center[2] + radius}},
      }
      local out = {}
      for _, entity in pairs(entities) do
        if entity.valid and entity.name ~= "character" then table.insert(out, serialize_entity(entity)) end
      end
      send_response(port, request, true, {center = center, radius = radius, entities = out, game_tick = game.tick})
    elseif op == "query_recipe" then
      send_response(port, request, true, {recipe = query_recipe(player, tostring(payload.name or "")), game_tick = game.tick})
    elseif op == "query_technology" then
      send_response(port, request, true, {technology = query_technology(player, tostring(payload.name or "")), game_tick = game.tick})
    elseif op == "act" then
      local action = tostring(payload.action or "")
      local handler = ACTIONS[action]
      if not handler then
        send_response(port, request, false, nil, "unsupported action: " .. action)
        return
      end
      handler(port, request, player, payload.params or {})
    else
      send_response(port, request, false, nil, "unsupported op: " .. tostring(op))
    end
  end)

  if handled then
    d.packets_handled = d.packets_handled + 1
    diag_log("packet " .. d.packets_seen .. " handled op=" .. tostring(request.op))
  else
    d.handler_errors = d.handler_errors + 1
    d.last_error = tostring(err)
    diag_log("packet " .. d.packets_seen .. " handler error: " .. tostring(err))
    send_response(port, request, false, nil, "handler error: " .. tostring(err))
  end
end

-- A first N calls are logged so the Factorio log proves whether the polling loop
-- actually started (on_nth_tick registration succeeded and keeps running).
local RECV_LOG_FIRST = 5
local recv_stage = 0

local function poll_udp()
  local d = diag()
  if recv_stage < RECV_LOG_FIRST then
    recv_stage = recv_stage + 1
    d.recv_calls = recv_stage
    diag_log("poll #" .. recv_stage .. " at tick " .. game.tick)
  end
  local ok, err = pcall(helpers.recv_udp)
  if ok then
    d.recv_ack = (d.recv_ack or 0) + 1
  else
    d.recv_errors = (d.recv_errors or 0) + 1
    d.last_error = "recv_udp: " .. tostring(err)
    if d.recv_errors <= 5 then
      diag_log("recv_udp error #" .. d.recv_errors .. ": " .. tostring(err))
    end
  end
end

commands.add_command(
  "gar-ai-diag",
  "Print GAR AI bridge diagnostics (packets seen, polling loop health).",
  function()
    local d = diag()
    game.print("[gar-ai-bridge] packets_seen=" .. d.packets_seen
      .. " handled=" .. d.packets_handled
      .. " bad_json=" .. d.packets_bad_json
      .. " bad_proto=" .. d.packets_bad_proto
      .. " handler_errors=" .. d.handler_errors
      .. " recv_ack=" .. tostring(d.recv_ack)
      .. " recv_errors=" .. tostring(d.recv_errors)
      .. " init_tick=" .. tostring(d.init_tick)
      .. " now_tick=" .. game.tick)
    if d.last_error then game.print("[gar-ai-bridge] last_error=" .. tostring(d.last_error)) end
  end
)

script.on_event(defines.events.on_udp_packet_received, handle_request)
script.on_configuration_changed(function()
  recv_stage = 0
  diag_log("configuration changed; polling restart at tick " .. game.tick)
end)
script.on_nth_tick(1, poll_udp)

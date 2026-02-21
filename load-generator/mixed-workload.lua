--------------------------------------------------------------------------------
-- Mixed Workload for Chaos Benchmark (Paper 4)
-- Based on DeathStarBench/socialNetwork/wrk2/scripts/social-network/mixed-workload.lua
-- Ratios: 40% compose_post, 30% read_home_timeline, 20% read_user_timeline, 10% follow_user
--------------------------------------------------------------------------------

local socket = require("socket")
local time = socket.gettime()*1000
math.randomseed(time)
math.random(); math.random(); math.random()

local charset = {'q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p', 'a', 's',
  'd', 'f', 'g', 'h', 'j', 'k', 'l', 'z', 'x', 'c', 'v', 'b', 'n', 'm', 'Q',
  'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O', 'P', 'A', 'S', 'D', 'F', 'G', 'H',
  'J', 'K', 'L', 'Z', 'X', 'C', 'V', 'B', 'N', 'M', '1', '2', '3', '4', '5',
  '6', '7', '8', '9', '0'}

local decset = {'1', '2', '3', '4', '5', '6', '7', '8', '9', '0'}

-- Configuration via environment variables
local max_user_index = tonumber(os.getenv("MAX_USER_INDEX")) or 962
local target_host = os.getenv("TARGET_HOST") or "nginx-thrift.social-network"
local target_port = os.getenv("TARGET_PORT") or "8080"
local base_url = "http://" .. target_host .. ":" .. target_port

local function stringRandom(length)
  if length > 0 then
    return stringRandom(length - 1) .. charset[math.random(1, #charset)]
  else
    return ""
  end
end

local function decRandom(length)
  if length > 0 then
    return decRandom(length - 1) .. decset[math.random(1, #decset)]
  else
    return ""
  end
end

local function compose_post()
  local user_index = math.random(0, max_user_index - 1)
  local username = "username_" .. tostring(user_index)
  local user_id = tostring(user_index)
  local text = stringRandom(256)
  local num_user_mentions = math.random(0, 5)
  local num_urls = math.random(0, 5)
  local num_media = math.random(0, 4)
  local media_ids = '['
  local media_types = '['

  for i = 0, num_user_mentions, 1 do
    local user_mention_id
    while (true) do
      user_mention_id = math.random(0, max_user_index - 1)
      if user_index ~= user_mention_id then
        break
      end
    end
    text = text .. " @username_" .. tostring(user_mention_id)
  end

  for i = 0, num_urls, 1 do
    text = text .. " http://" .. stringRandom(64)
  end

  for i = 0, num_media, 1 do
    local media_id = decRandom(18)
    media_ids = media_ids .. "\"" .. media_id .. "\","
    media_types = media_types .. "\"png\","
  end

  media_ids = media_ids:sub(1, #media_ids - 1) .. "]"
  media_types = media_types:sub(1, #media_types - 1) .. "]"

  local method = "POST"
  local path = base_url .. "/wrk2-api/post/compose"
  local headers = {}
  local body
  headers["Content-Type"] = "application/x-www-form-urlencoded"
  if num_media then
    body = "username=" .. username .. "&user_id=" .. user_id ..
        "&text=" .. text .. "&media_ids=" .. media_ids ..
        "&media_types=" .. media_types .. "&post_type=0"
  else
    body = "username=" .. username .. "&user_id=" .. user_id ..
        "&text=" .. text .. "&media_ids=" .. "&post_type=0"
  end

  return wrk.format(method, path, headers, body)
end

local function read_user_timeline()
  local user_id = tostring(math.random(0, max_user_index - 1))
  local start = tostring(math.random(0, 100))
  local stop = tostring(start + 10)

  local args = "user_id=" .. user_id .. "&start=" .. start .. "&stop=" .. stop
  local method = "GET"
  local headers = {}
  headers["Content-Type"] = "application/x-www-form-urlencoded"
  local path = base_url .. "/wrk2-api/user-timeline/read?" .. args
  return wrk.format(method, path, headers, nil)
end

local function read_home_timeline()
  local user_id = tostring(math.random(0, max_user_index - 1))
  local start = tostring(math.random(0, 100))
  local stop = tostring(start + 10)

  local args = "user_id=" .. user_id .. "&start=" .. start .. "&stop=" .. stop
  local method = "GET"
  local headers = {}
  headers["Content-Type"] = "application/x-www-form-urlencoded"
  local path = base_url .. "/wrk2-api/home-timeline/read?" .. args
  return wrk.format(method, path, headers, nil)
end

local function follow_user()
  local user_id = tostring(math.random(0, max_user_index - 1))
  local followee_id
  while (true) do
    followee_id = math.random(0, max_user_index - 1)
    if tonumber(user_id) ~= followee_id then
      break
    end
  end
  followee_id = tostring(followee_id)

  local method = "POST"
  local path = base_url .. "/wrk2-api/user/follow"
  local headers = {}
  headers["Content-Type"] = "application/x-www-form-urlencoded"
  local body = "user_id=" .. user_id .. "&followee_id=" .. followee_id
  return wrk.format(method, path, headers, body)
end

-- Paper 4 ratios: 40% compose, 30% read_home, 20% read_user, 10% follow
request = function()
  cur_time = math.floor(socket.gettime())
  local compose_post_ratio       = 0.40
  local read_home_timeline_ratio = 0.30
  local read_user_timeline_ratio = 0.20
  -- follow_user_ratio            = 0.10 (remainder)

  local coin = math.random()
  if coin < compose_post_ratio then
    return compose_post()
  elseif coin < compose_post_ratio + read_home_timeline_ratio then
    return read_home_timeline()
  elseif coin < compose_post_ratio + read_home_timeline_ratio + read_user_timeline_ratio then
    return read_user_timeline()
  else
    return follow_user()
  end
end

local removed = false

function Header(header)
  if not removed and header.level == 1 then
    removed = true
    return {}
  end
end

-- Real excerpt from PathOfBuilding src/Data/Bases/body.lua at
-- commit 9e6ea223e5921abd00bab36f246072e353140a58.
local itemBases = ...

itemBases["Plate Vest"] = {
	type = "Body Armour",
	subType = "Armour",
	socketLimit = 6,
	tags = { armour = true, body_armour = true, default = true, str_armour = true, },
	influenceTags = { shaper = "body_armour_shaper", elder = "body_armour_elder", adjudicator = "body_armour_adjudicator", basilisk = "body_armour_basilisk", crusader = "body_armour_crusader", eyrie = "body_armour_eyrie", cleansing = "body_armour_cleansing", tangle = "body_armour_tangle" },
	implicitModTypes = { },
	armour = { ArmourBaseMin = 19, ArmourBaseMax = 27, MovementPenalty = 3, },
	req = { str = 12, },
}
itemBases["Chestplate"] = {
	type = "Body Armour",
	subType = "Armour",
	socketLimit = 6,
	tags = { armour = true, body_armour = true, default = true, str_armour = true, },
	influenceTags = { shaper = "body_armour_shaper", elder = "body_armour_elder", adjudicator = "body_armour_adjudicator", basilisk = "body_armour_basilisk", crusader = "body_armour_crusader", eyrie = "body_armour_eyrie", cleansing = "body_armour_cleansing", tangle = "body_armour_tangle" },
	implicitModTypes = { },
	armour = { ArmourBaseMin = 68, ArmourBaseMax = 89, MovementPenalty = 5, },
	req = { level = 6, str = 25, },
}
itemBases["Copper Plate"] = {
	type = "Body Armour",
	subType = "Armour",
	socketLimit = 6,
	tags = { armour = true, body_armour = true, default = true, str_armour = true, },
	influenceTags = { shaper = "body_armour_shaper", elder = "body_armour_elder", adjudicator = "body_armour_adjudicator", basilisk = "body_armour_basilisk", crusader = "body_armour_crusader", eyrie = "body_armour_eyrie", cleansing = "body_armour_cleansing", tangle = "body_armour_tangle" },
	implicitModTypes = { },
	armour = { ArmourBaseMin = 176, ArmourBaseMax = 221, MovementPenalty = 5, },
	req = { level = 17, str = 53, },
}

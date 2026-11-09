# The GPU VM for Ollama: no public IP, reachable on 11434 from the apps subnet only (network.tf).

resource "azurerm_network_interface" "ollama" {
  name                = "${var.name}-ollama-nic"
  location            = var.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags

  ip_configuration {
    name                          = "private"
    subnet_id                     = azurerm_subnet.ollama.id
    private_ip_address_allocation = "Dynamic"
  }
}

resource "azurerm_linux_virtual_machine" "ollama" {
  name                            = "${var.name}-ollama"
  location                        = var.location
  resource_group_name             = azurerm_resource_group.main.name
  size                            = var.ollama_vm_size
  admin_username                  = "governed"
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.ollama.id]
  custom_data = base64encode(templatefile("${path.module}/../shared/ollama-cloud-init.sh.tftpl", {
    ollama_version = var.ollama_version
    models         = var.ollama_models
  }))
  tags = var.tags

  admin_ssh_key {
    username   = "governed"
    public_key = var.admin_ssh_public_key
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = 256 # the models
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }
}

resource "azurerm_virtual_machine_extension" "gpu_driver" {
  name                       = "NvidiaGpuDriverLinux"
  virtual_machine_id         = azurerm_linux_virtual_machine.ollama.id
  publisher                  = "Microsoft.HpcCompute"
  type                       = "NvidiaGpuDriverLinux"
  type_handler_version       = "1.6"
  auto_upgrade_minor_version = true
  tags                       = var.tags
}

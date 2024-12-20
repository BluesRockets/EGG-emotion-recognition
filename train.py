import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, Subset, random_split
from sklearn.model_selection import StratifiedKFold, train_test_split
import time

subjects = 15
segment_length = 6
total_epoch = 100


class EEGDataset(Dataset):
    def __init__(self, data, labels):
        self.data = data
        self.labels = labels

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        x = self.data[idx]
        y = self.labels[idx]
        return torch.tensor(x, dtype=torch.float32), torch.tensor(y, dtype=torch.float32)


# Define the Base Convolutional Network
class ConvNet(nn.Module):
    def __init__(self, input_dim):
        super(ConvNet, self).__init__()
        # Changed input_dim[2] to input_dim[0] to get the correct number of channels
        self.conv1 = nn.Conv2d(input_dim[2], 64, kernel_size=5, padding=2)
        self.conv2 = nn.Conv2d(64, 128, kernel_size=4, padding=1)
        self.conv3 = nn.Conv2d(128, 256, kernel_size=4, padding=1)
        self.conv4 = nn.Conv2d(256, 64, kernel_size=1)
        self.pool = nn.MaxPool2d(2, 2)
        self.flatten = nn.Flatten()
        # Calculate the correct input size for dense1 dynamically
        self.dense1 = nn.Linear(self._get_conv_output_size(input_dim), 512)

    def _get_conv_output_size(self, input_dim):
        # Create a dummy input tensor with the correct shape (channels, height, width)
        # Here you should change to the correct order (batch_size, channels, height, width)
        dummy_input = torch.zeros(1, input_dim[2], input_dim[0], input_dim[1])  # Change the order to (1, 4, 8, 9)
        # Pass the dummy input through the convolutional layers
        x = self.conv1(dummy_input)
        x = self.conv2(x)
        x = self.conv3(x)
        x = self.conv4(x)
        x = self.pool(x)
        x = self.flatten(x)
        # Return the size of the flattened output
        return x.size(1)

    def forward(self, x):
        x = torch.relu(self.conv1(x))
        x = torch.relu(self.conv2(x))
        x = torch.relu(self.conv3(x))
        x = torch.relu(self.conv4(x))
        x = self.pool(x)
        x = self.flatten(x)
        x = torch.relu(self.dense1(x))
        x = x.unsqueeze(1)  # Reshape to (batch_size, 1, 512)
        return x


# Define the Combined LSTM Model
class EEGNet(nn.Module):
    def __init__(self, input_dim, num_classes=4):
        super(EEGNet, self).__init__()
        self.base_network = ConvNet(input_dim)
        self.lstm = nn.LSTM(512, 128, batch_first=True)
        self.out = nn.Linear(128, num_classes)

    def forward(self, x):
        # x is a list of 6 input tensors
        x = torch.cat([self.base_network(inp) for inp in x], dim=1)
        _, (h_n, _) = self.lstm(x)  # h_n is the hidden state from LSTM
        out = self.out(h_n[-1])  # Use the last hidden state
        return out


if __name__ == '__main__':
    # Hyperparameters and configurations
    num_classes = 4
    batch_size = 128
    img_rows, img_cols, num_chan = 8, 9, 4
    seed = 7
    np.random.seed(seed)
    torch.manual_seed(seed)

    # Check GPU availability
    cuda_available = torch.cuda.is_available()
    mps_available = torch.backends.mps.is_available()
    if cuda_available:
        device = torch.device("cuda:0")
    elif mps_available:
        device = torch.device("mps")
    else:
        device = torch.device("cpu")
    print(f"Using device: {device}")

    # Data loading
    falx = np.load("./features/0_segmented_x_89.npy")
    y = np.load("./features/0_segmented_y_89.npy")

    num_segments = falx.shape[1]
    one_y_1 = y.astype(int)  # Convert to integers
    one_y_1 = np.eye(num_classes)[one_y_1]  # Convert to one-hot encoded format

    # Process each subject independently
    start = time.time()
    one_falx_1 = falx.reshape((-1, segment_length, img_rows, img_cols, 5))
    one_falx = one_falx_1[:, :, :, :, 1:5]  # Only use four frequency bands

    X_train, X_test, y_train, y_test = train_test_split(one_falx, one_y_1, test_size=0.3)

    dataset = EEGDataset(X_train, y_train)
    np.save('./features/x_test.npy', X_test)
    np.save('./features/y_test.npy', y_test)

    # Create DataLoader for training and testing
    train_loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=4,
                              pin_memory=True)

    model = EEGNet((img_rows, img_cols, 4)).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters())

    # Mixed precision training
    scaler = torch.amp.GradScaler()

    # Training Loop
    model.train()
    for epoch in range(total_epoch):
        epoch_start = time.time()
        running_loss = 0.0
        for batch_x, batch_y in train_loader:
            # Change the permutation order to (0, 2, 3, 1) to match the expected channel dimension
            inputs = [batch_x[:, i].permute(0, 3, 1, 2).to(device, non_blocking=True) for i in range(6)]
            labels = batch_y.argmax(dim=1).to(device, non_blocking=True)

            optimizer.zero_grad()

            # Mixed precision training
            with torch.amp.autocast("cuda"):
                outputs = model(inputs)
                loss = criterion(outputs, labels)

            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            running_loss += loss.item()

        epoch_end = time.time()
        # Print the average loss for the epoch and the time taken
        print(
            f"Epoch [{epoch + 1}/{total_epoch}], Loss: {running_loss / len(train_loader):.4f}, Time: {epoch_end - epoch_start:.2f} seconds")
        if running_loss / len(train_loader) < 0.005: #adopt early stop
            break

    torch.save(model.state_dict(), './results/model.pt')

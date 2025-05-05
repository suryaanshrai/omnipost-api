from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.utils import timezone
import boto3, requests, os, json
from zxcvbn import zxcvbn
from django_rq import get_queue
from omnipost_api.fernet import FernetEncryptor
import os
import magic


def validate_video_file(file):
    """
    Validate the uploaded video file.
    - Checks extension
    - Checks MIME type
    """
    # List of valid video file extensions
    valid_extensions = ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm']
    
    # Get the file extension
    ext = os.path.splitext(file.name)[1].lower()
    
    if ext not in valid_extensions:
        raise ValidationError('Unsupported file extension. Please upload a video file.')
    
    # Check MIME type (requires python-magic package)
    file_mime = magic.from_buffer(file.read(1024), mime=True)
    file.seek(0)  # Reset file pointer
    
    if not file_mime.startswith('video/'):
        raise ValidationError('Uploaded file is not a valid video.')


class User(AbstractUser):
    pass


class Platform(models.Model):
    """
    ## Configuration for a social media platform, eg. the details needed to connect to the platform's API.
    
    - Every platform should be configured in the following manner:
    ```python
    config = {
        "INSTANCE": {
            "key1": "value1",
            "key2": "value2",
            ...
        },
        "ACTIONS": {
            "POST_IMAGE": [
                [request1, expected_response_code1, variable_mapping1],
                [request2, expected_response_code2, variable_mapping2],
                ...
            ],
            "POST_VIDEO": [
                [request1, expected_response_code1, variable_mapping1],
                ...
            ],
            "POST_TEXT": [
                [request1, expected_response_code1, variable_mapping1],
                ...
            ],
            "POST_SHORT_FORM_VIDEO": [
                [request1, expected_response_code1, variable_mapping1],
                ...
            ],
            "POST_STORIES": [
                [request1, expected_response_code1, variable_mapping1],
                ...
            ],
        }
    }
    ```
    
    - A `request` must be of the following format:
    ```
    {
        "base_url": "https://api.example.com",
        "endpoint": "/path/to/endpoint",
        "method": "POST",
        "headers": {
            "Authorization": "Bearer ACCESS_TOKEN",
            "Content-Type": "application/json",
            ...
        },
        "params": {
            "key1": "value1",
            "key2": "value2",
            ...
        },
        "payload": {
            "key1": "value1",
            "key2": "value2",
            ...
        }
    }
    ```
    """
    name = models.CharField(max_length=100, blank=False)
    config = models.JSONField(default=dict, blank=True, null=True)
    # The configs field would contain the configuration details needed to connect to the platform's API
    
    
    def __str__(self):
        return self.name
    
    
    
class PlatformInstance(models.Model):
    """
    An instance of a social media platform, e.g. a specific Twitter account
    """
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    credentials = models.JSONField(default=dict, blank=True, null=True)
    salt = models.BinaryField(blank=True, null=True)
    
    
    def save(self, password=None, *args, **kwargs):
        # Initialize credentials based on platform configs
        # if not password:
        #     raise ValidationError("Password is required to encrypt credentials.")
        
        # instance_config = self.platform.config["INSTANCE"]
        # for key in instance_config.keys():
        #     try:
        #         self.credentials[key] = self.credentials[key] # Checks if the key exists or not
        #     except KeyError:
        #         self.credentials[key] = ''
        
        # pswd_check = zxcvbn(password)
        # if pswd_check['score'] < 3 or pswd_check["feedback"]["warning"] or pswd_check["feedback"]["suggestions"]:
        #     raise ValidationError(f"Weak password:{pswd_check["feedback"]["warning"]} {" ".join(pswd_check["feedback"]["suggestions"])}")
            
        # encryptor = FernetEncryptor(password=password)
        # self.salt = encryptor.salt
        # self.credentials = encryptor.encrypt_dict(self.credentials)
            
        super().save(*args, **kwargs)
    
    def get_credentials(self, password=None):
        # if password is None:
        #     raise ValueError("Password is required to decrypt credentials.")
        # else:
        #     encryptor = FernetEncryptor(salt=self.salt, password=password)
        #     decrypted_credentials = encryptor.decrypt_dict_keys(self.credentials)
        #     return decrypted_credentials
        return self.credentials
        
    def __str__(self):
        return f"{self.platform.name} - {self.user.username}"
        
        
        
class PostBase(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    platform_instances = models.ManyToManyField(PlatformInstance)
    created_at = models.DateTimeField(auto_now_add=True)
    post_configs = models.JSONField(default=dict, blank=True, null=True)
    schedule = models.DateTimeField(blank=True, null=True)
    
    class Meta:
        abstract = True
    
    def run_action(
        self,
        action: str,
        platform_instance: PlatformInstance, 
        password: str = None,
        delay: int = 5,
    ) -> None:
        """
        Execute an action on a platform instance
        
        Args:
            action (str): The action to execute
            platform_instance (PlatformInstance): The platform instance to execute the action on
            password (str): The password to decrypt the credentials
            delay (int): The delay between each request (in seconds)
            max_retries (int): The maximum number of retries
        Returns:
            bool: True if the action was executed successfully, False otherwise
        Raises:
            ValueError: If the action is not defined in the platform instance
        """
        if action not in platform_instance.platform.config["ACTIONS"]:
            raise ValueError(f"Action '{action}' not defined in platform {platform_instance.platform.name}.")
            
        a = platform_instance.platform.config["ACTIONS"][action]
        iteration = 1
        for request, expected_response_code, variable_mapping in a:            
            q = get_queue('default')
            q.enqueue_at(
                timezone.now()+timezone.timedelta(seconds=delay*iteration), 
                send_request,
                post_object=self,
                platform_instance=platform_instance,
                request=request,
                expected_response_code=expected_response_code,
                variable_mapping=variable_mapping,
                password=password,
            )
            iteration += 1
            
    def run_action_on_all_platforms(
        self, 
        action: str, 
        password: str = None,
        delay: int = 5,
        ) -> None:
        """
        Execute an action on all platform instances
        
        Args:
            action (str): The action to execute
            password (str): The password to decrypt the credentials
            delay (int): The delay between each request (in seconds)
        """
        for platform_instance in self.platform_instances.all():
            self.run_action(action=action, platform_instance=platform_instance, password=password, delay=delay)
    
    def save_to_aws_s3(self, file_path, file_name):
        """
        Save a file to the AWS cloud for public access
        """
        try:
            client = boto3.client(
                's3',
                aws_access_key_id=os.environ.get("AWS_ACCESS_KEY"),
                aws_secret_access_key=os.environ.get("AWS_SECRET_KEY")
            )

            client.upload_file(file_path, 'omnipost-images', file_name)
        
        except Exception as e:
            raise ValueError(f"Failed to upload file to cloud: {e}")

        return True
        
        
        
class PostText(PostBase):
    """
    A text post
    """
    text = models.TextField(blank=False)
    
    def save(self, *args, **kwargs):
        super().save()
        
        if self.post_configs == {}: 
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"] = {
                    "TEXT": self.text,
                }
            super().save()

        
        
class PostImage(PostBase):
    """
    A normal post. text and image
    """
    caption = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='media/', blank=True, null=True)
    image_url = models.URLField(blank=True, null=True)
    
    def save(self, *args, **kwargs):
        super().save()
        
        if self.post_configs == {}: 
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"] = {
                    "CAPTION": self.caption,
                    "IMAGE_URL": self.image_url
                }
            super().save()
        
        if self.image and self.image_url is None:
            self.save_to_aws_s3(self.image.path, self.image.name)
            cloud_url = os.environ.get('BUCKET_URL')
            self.image_url = f"{cloud_url}/{self.image.name}"
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"]["IMAGE_URL"] = self.image_url
            super().save()
            
class PostVideo(PostBase):
    caption = models.TextField(blank=True, null=True)
    video = models.FileField(upload_to='media/', blank=True, null=True)
    video_url = models.URLField(blank=True, null=True)
    
    def save(self, *args, **kwargs):
        super().save()
        
        if self.post_configs == {}: 
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"] = {
                    "CAPTION": self.caption,
                    "VIDEO_URL": self.video_url
                }
            super().save()
        
        if self.video and self.video_url is None:
            self.save_to_aws_s3(self.video.path, self.video.name)
            cloud_url = os.environ.get('BUCKET_URL')
            self.video_url = f"{cloud_url}/{self.video.name}"
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"]["VIDEO_URL"] = self.video_url
            super().save()

class ShortFormVideo(PostBase):
    """
    A short video, like reels, for any platform in general
    """
    caption = models.TextField(blank=True, null=True)
    video = models.FileField(upload_to='media/', blank=True, null=True)
    video_url = models.URLField(blank=True, null=True)
    
    def save(self, *args, **kwargs):
        super().save()
        
        if self.post_configs == {}: 
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"] = {
                    "CAPTION": self.caption,
                    "VIDEO_URL": self.video_url
                }
            super().save()
        
        if self.video and self.video_url is None:
            self.save_to_aws_s3(self.video.path, self.video.name)
            cloud_url = os.environ.get('BUCKET_URL')
            self.video_url = f"{cloud_url}/{self.video.name}"
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"]["VIDEO_URL"] = self.video_url
            super().save()
            


class StoryImage(PostBase):
    """
    Image story/status for any platform in general
    """
    image = models.ImageField(upload_to='media/', blank=True, null=True)
    image_url = models.URLField(blank=True, null=True)
    
    class Meta:
        verbose_name = "Story Image"
        verbose_name_plural = "Story Images"
    
    def save(self, *args, **kwargs):
        super().save()
        
        if self.post_configs == {}: 
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"] = {
                    "IMAGE_URL": self.image_url
                }
            super().save()
        
        if self.image and self.image_url is None:
            self.save_to_aws_s3(self.image.path, self.image.name)
            cloud_url = os.environ.get('BUCKET_URL')
            self.image_url = f"{cloud_url}/{self.image.name}"
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"]["IMAGE_URL"] = self.image_url
            super().save()


class StoryVideo(PostBase):
    """
    Video story/status for any platform in general
    """
    video = models.FileField(upload_to='media/', blank=True, null=True, validators=[validate_video_file])
    video_url = models.URLField(blank=True, null=True)
    
    class Meta:
        verbose_name = "Story Video"
        verbose_name_plural = "Story Videos"
    
    def save(self, *args, **kwargs):
        super().save()
        
        if self.post_configs == {}: 
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"] = {
                    "VIDEO_URL": self.video_url
                }
            super().save()
        
        if self.video and self.video_url is None:
            self.save_to_aws_s3(self.video.path, self.video.name)
            cloud_url = os.environ.get('BUCKET_URL')
            self.video_url = f"{cloud_url}/{self.video.name}"
            for platform in Platform.objects.all():
                self.post_configs[f"{platform.name}"]["VIDEO_URL"] = self.video_url
            super().save()

class Doc(models.Model):
    """
    Documents for any platform in general
    """
    title = models.CharField(max_length=100, blank=True, null=True)
    youtube_video = models.URLField(blank=True, null=True)
    custom_doc = models.TextField(blank=True, null=True)
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, blank=True, null=True)
    
class Notification(models.Model):
    """
    Any notifications from the platform
    """
    platform_instance = models.ForeignKey(PlatformInstance, on_delete=models.CASCADE, blank=True, null=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True)
    notification = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    error = models.BooleanField(default=False)
    
    def __str__(self):
        return f"{self.platform_instance.platform.name} - {self.notification}"



def replace_keys(request: dict, keys: dict) -> dict:
    """
    Replace keys in the request dictionary with values from the keys dictionary.
    """
    request_string = json.dumps(request)
    for key, value in keys.items():
        request_string = request_string.replace(f"{key}",f"{value}")
    request = json.loads(request_string)
    return request

def send_request(
    post_object: PostBase,
    platform_instance: PlatformInstance,
    request: dict,
    expected_response_code: int,
    variable_mapping: dict,
    password: str,
    ) -> bool:

    post_object.refresh_from_db()
    request = replace_keys(request, platform_instance.get_credentials(password=password))
    request = replace_keys(request, post_object.post_configs[platform_instance.platform.name])
    
    response = requests.request(
        request["method"],
        request["base_url"] + request["endpoint"],
        headers=request["headers"],
        params=request["params"],
        json=request["payload"]
    )

    # with open("request.txt", "a") as f:
    #     f.write(f"Request: {request}\n")
    #     f.write(f"Response: {response.text}\n")
    #     f.write(f"Status Code: {response.status_code}\n")
    #     f.write("\n\n")
    # print(request)
    if response.status_code != expected_response_code:
        Notification(
            platform_instance=platform_instance,
            user=post_object.user,
            notification=f"Something went wrong. {response.text}",
            error=True
        ).save()
        raise ValueError(f"Unexpected response code: {response.status_code}. Failed to create post. {response.text}")

    for key, value in variable_mapping.items():
        if key == 'terminal_request':
            Notification(
                platform_instance=platform_instance,
                user=post_object.user,
                notification=f"Post created successfully",
            ).save()
            continue
        post_object.post_configs[platform_instance.platform.name][value] = response.json()[key]
    post_object.save()

    
    return True